"""Удалённый доступ агентов: MCP по HTTP (Streamable HTTP) для других машин пользователя (ARCHITECTURE.md §8.4).

Слушает все интерфейсы, но пропускает только:
  * адреса Tailscale (100.64.0.0/10, fd7a:115c:a1e0::/48) и локальный компьютер;
  * запросы с заголовком Authorization: Bearer <токен из settings.json>.
В интернет напрямую не открывается: без Tailscale до порта никто не дойдёт, а без токена — не пройдёт.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time

from .. import settings

log = logging.getLogger("xlauncher.remote")

ALLOWED_NETS = [ipaddress.ip_network(n) for n in
                ("127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48")]
DEFAULT = {"enabled": False, "port": 47111, "token": ""}


def config() -> dict:
    return {**DEFAULT, **(settings.load().get("remote") or {})}


def save(values: dict) -> dict:
    cfg = {**config(), **values}
    settings.save({"remote": cfg})
    return cfg


def new_token() -> str:
    return "xl_" + secrets.token_urlsafe(32)


def tailscale_ips() -> list[str]:
    """IP этого компьютера в tailnet (по интерфейсам, при необходимости — через tailscale.exe)."""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0].split("%")[0]
            ip = ipaddress.ip_address(addr)
            if ip in ALLOWED_NETS[2] or ip in ALLOWED_NETS[3]:
                ips.add(addr)
    except OSError:
        pass
    if not any(":" not in i for i in ips):
        exe = shutil.which("tailscale") or (r"C:\Program Files\Tailscale\tailscale.exe" if sys.platform == "win32" else None)
        if exe:
            try:
                flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5, creationflags=flags)
                ips |= {x.strip() for x in out.stdout.split() if x.strip()}
            except (OSError, subprocess.SubprocessError):
                pass
    return sorted(ips, key=lambda x: (":" in x, x))


def client_allowed(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address((host or "").split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in n for n in ALLOWED_NETS)


class Guard:
    """ASGI-обёртка: адрес клиента из tailnet/локальный и верный токен."""

    def __init__(self, app, token_getter):
        self.app = app
        self.token_getter = token_getter

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        client = (scope.get("client") or ("", 0))[0]
        if not client_allowed(client):
            log.warning("удалённый доступ: отклонён адрес %s", client)
            return await _deny(send, 403, "адрес не из tailnet")
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        token = self.token_getter()
        if not token or not hmac.compare_digest(auth, f"Bearer {token}"):
            log.warning("удалённый доступ: неверный токен с %s", client)
            return await _deny(send, 401, "нужен токен: Authorization: Bearer <токен>")
        return await self.app(scope, receive, send)


async def _deny(send, code: int, text: str) -> None:
    body = text.encode()
    await send({"type": "http.response.start", "status": code,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class RemoteServer:
    """MCP по HTTP в отдельном потоке; перезапускается при смене настроек."""

    def __init__(self) -> None:
        self._server = None
        self._thread: threading.Thread | None = None
        self.error: str | None = None
        self.port: int | None = None

    @property
    def running(self) -> bool:
        return bool(self._server and self._thread and self._thread.is_alive())

    def apply(self) -> None:
        cfg = config()
        want = cfg["enabled"] and cfg["token"]
        if self.running and (not want or self.port != int(cfg["port"])):
            self.stop()
        if want and not self.running:
            self.start(int(cfg["port"]))

    def start(self, port: int) -> None:
        import uvicorn
        from mcp.server.transport_security import TransportSecuritySettings

        from .. import mcp_server

        self.error = None
        app = mcp_server.build().streamable_http_app(
            host="0.0.0.0",
            # Защита от DNS rebinding рассчитана на работу без авторизации; здесь каждый запрос — с токеном.
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        guarded = Guard(app, lambda: config()["token"])
        self._server = uvicorn.Server(uvicorn.Config(guarded, host="0.0.0.0", port=port, log_level="warning",
                                                     log_config=None, lifespan="on"))
        self.port = port

        def run() -> None:
            try:
                self._server.run()
            except BaseException as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"
                log.exception("удалённый доступ")

        self._thread = threading.Thread(target=run, name="remote-mcp", daemon=True)
        self._thread.start()
        for _ in range(100):
            if self._server.started or not self._thread.is_alive():
                break
            time.sleep(0.05)
        if not self._server.started and not self.error:
            self.error = f"порт {port} занят или недоступен"
        if self._server.started:
            log.info("удалённый доступ включён: порт %s", port)

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(5)
        self._server = self._thread = None
        log.info("удалённый доступ выключен")

    def status(self) -> dict:
        cfg = config()
        ips = tailscale_ips()
        urls = [f"http://{'[' + ip + ']' if ':' in ip else ip}:{cfg['port']}/mcp" for ip in ips]
        return {"enabled": cfg["enabled"], "running": self.running, "port": cfg["port"], "token": cfg["token"],
                "error": self.error, "tailscale_ips": ips, "urls": urls, "host": socket.gethostname()}


server = RemoteServer()
