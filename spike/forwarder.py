"""Локальный прокси-форвардер.

Chromium подключается к 127.0.0.1:<порт> как к обычному HTTP-прокси без авторизации.
Форвардер открывает туннель через вышестоящий прокси (SOCKS5 или HTTP, с логином/паролем
или без) и передаёт ему имя хоста, а не IP: DNS разрешается на стороне прокси.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

HEAD_LIMIT = 64 * 1024
CONNECT_TIMEOUT = 20


@dataclass
class Upstream:
    scheme: str  # "socks5" | "http"
    host: str
    port: int
    user: str | None = None
    password: str | None = None

    def masked(self) -> str:
        auth = f"{self.user}:***@" if self.user else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"


def parse_proxy(s: str) -> Upstream | None:
    """Разбирает строку прокси.

    socks5://user:pass@host:port, http://user:pass@host:port,
    user:pass@host:port, host:port:user:pass, host:port. Пустая строка → None.
    """
    s = (s or "").strip()
    if not s:
        return None
    if "://" not in s and "@" in s:
        s = "socks5://" + s
    if "://" in s:
        u = urlsplit(s)
        scheme = u.scheme.lower()
        if scheme in ("socks5h", "socks"):
            scheme = "socks5"
        if scheme not in ("socks5", "http"):
            raise ValueError(f"Неподдерживаемая схема прокси: {u.scheme}")
        if not u.hostname or not u.port:
            raise ValueError("В строке прокси нет хоста или порта")
        return Upstream(
            scheme,
            u.hostname,
            u.port,
            unquote(u.username) if u.username else None,
            unquote(u.password) if u.password else None,
        )
    parts = s.split(":")
    if len(parts) == 2:
        return Upstream("socks5", parts[0], int(parts[1]))
    if len(parts) == 4:
        return Upstream("socks5", parts[0], int(parts[1]), parts[2], parts[3])
    raise ValueError("Не удалось разобрать строку прокси")


async def _read_head(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """Читает HTTP-заголовки до пустой строки. Возвращает (заголовки, остаток)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await reader.read(4096)
        if not chunk:
            raise ConnectionError("Соединение закрыто до конца заголовков")
        buf += chunk
        if len(buf) > HEAD_LIMIT:
            raise ConnectionError("Слишком длинные заголовки")
    head, rest = buf.split(b"\r\n\r\n", 1)
    return head, rest


async def _socks5_connect(up: Upstream, host: str, port: int):
    reader, writer = await asyncio.open_connection(up.host, up.port)
    methods = b"\x00\x02" if up.user else b"\x00"
    writer.write(b"\x05" + bytes([len(methods)]) + methods)
    await writer.drain()
    ver, method = await reader.readexactly(2)
    if ver != 5 or method == 0xFF:
        raise ConnectionError("SOCKS5: прокси отверг методы авторизации")
    if method == 2:
        u = (up.user or "").encode()
        p = (up.password or "").encode()
        writer.write(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
        await writer.drain()
        _, status = await reader.readexactly(2)
        if status != 0:
            raise ConnectionError("SOCKS5: неверный логин или пароль")
    try:
        ip = ipaddress.ip_address(host)
        addr = (b"\x01" if ip.version == 4 else b"\x04") + ip.packed
    except ValueError:
        h = host.encode("idna")
        addr = b"\x03" + bytes([len(h)]) + h
    writer.write(b"\x05\x01\x00" + addr + port.to_bytes(2, "big"))
    await writer.drain()
    _, rep, _, atyp = await reader.readexactly(4)
    if rep != 0:
        raise ConnectionError(f"SOCKS5: ошибка подключения к {host}:{port} (код {rep})")
    if atyp == 1:
        await reader.readexactly(4)
    elif atyp == 4:
        await reader.readexactly(16)
    elif atyp == 3:
        (n,) = await reader.readexactly(1)
        await reader.readexactly(n)
    await reader.readexactly(2)
    return reader, writer


async def _http_connect(up: Upstream, host: str, port: int):
    reader, writer = await asyncio.open_connection(up.host, up.port)
    target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
    if up.user:
        token = base64.b64encode(f"{up.user}:{up.password or ''}".encode()).decode()
        req += f"Proxy-Authorization: Basic {token}\r\n"
    writer.write((req + "\r\n").encode())
    await writer.drain()
    head, rest = await _read_head(reader)
    status = head.split(b"\r\n", 1)[0].split(b" ")
    if len(status) < 2 or status[1] != b"200":
        raise ConnectionError(f"HTTP-прокси: {head.split(b'\r\n', 1)[0].decode(errors='replace')}")
    if rest:
        reader.feed_data(rest)  # в норме пусто
    return reader, writer


async def open_tunnel(up: Upstream | None, host: str, port: int):
    coro = (
        asyncio.open_connection(host, port)
        if up is None
        else _socks5_connect(up, host, port)
        if up.scheme == "socks5"
        else _http_connect(up, host, port)
    )
    return await asyncio.wait_for(coro, CONNECT_TIMEOUT)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _split_hostport(s: str, default_port: int) -> tuple[str, int]:
    if s.startswith("["):  # [ipv6]:port
        host, _, rest = s[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    if s.count(":") == 1:
        host, port = s.split(":")
        return host, int(port)
    return s, default_port


class Forwarder:
    def __init__(self, upstream: Upstream | None):
        self.upstream = upstream
        self.server: asyncio.base_events.Server | None = None
        self.port: int | None = None
        self.errors: list[str] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass

    async def _handle(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        try:
            head, rest = await _read_head(cr)
            lines = head.split(b"\r\n")
            method, target, version = lines[0].decode("latin-1").split(" ", 2)
            if method.upper() == "CONNECT":
                host, port = _split_hostport(target, 443)
                ur, uw = await open_tunnel(self.upstream, host, port)
                cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await cw.drain()
                if rest:
                    uw.write(rest)
            else:
                u = urlsplit(target)
                if u.scheme != "http" or not u.hostname:
                    cw.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                    await cw.drain()
                    cw.close()
                    return
                host, port = u.hostname, u.port or 80
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                headers = [
                    h
                    for h in lines[1:]
                    if not h.lower().startswith((b"proxy-", b"connection:"))
                ]
                new_head = [f"{method} {path} {version}".encode("latin-1"), *headers, b"Connection: close"]
                ur, uw = await open_tunnel(self.upstream, host, port)
                uw.write(b"\r\n".join(new_head) + b"\r\n\r\n" + rest)
            await uw.drain()
            await asyncio.gather(_pipe(cr, uw), _pipe(ur, cw))
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{type(e).__name__}: {e}")
            try:
                cw.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                await cw.drain()
            except Exception:
                pass
            try:
                cw.close()
            except Exception:
                pass
