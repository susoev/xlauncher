"""Раннер профиля: python -m xlauncher.runner <profile_id> [--confirm-country]

Отдельный процесс на каждый запущенный профиль (ARCHITECTURE.md §2.1, §5):
  1. поднимает локальный прокси-форвардер;
  2. проверяет прокси, IP и гео; при смене страны без подтверждения — останавливается;
  3. готовит профиль к запуску на этом компьютере (ключ кук, lock-файлы);
  4. запускает браузер и ждёт его закрытия;
  5. пока браузер открыт — раз в N минут проверяет внешний IP и пишет историю.

Состояние — в run/profile-<id>.json (см. core/runstate.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time

from . import __version__, paths, settings
from .core import engine, iphistory, portability, prefs, profiles, runstate
from .core.proxy import Forwarder, ProxyError, external_ip, parse_proxy

log = logging.getLogger("xlauncher.runner")

EXIT_OK, EXIT_PROXY, EXIT_CONFIRM, EXIT_ERROR = 0, 2, 3, 4


class Stop(Exception):
    def __init__(self, code: int, status: str, error: str | None = None, **extra):
        super().__init__(error or status)
        self.code, self.status, self.error, self.extra = code, status, error, extra


def _close_windows(pid: int) -> int:
    """Посылает WM_CLOSE всем видимым окнам процесса — как нажатие крестика. Возвращает число окон."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    found: list = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    for h in found:
        user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE
    return len(found)


def _close_browser(proc: subprocess.Popen) -> None:
    """Закрывает окна браузера (как крестиком), через 20 с — принудительно.

    Мягкое закрытие важно: браузер успевает сохранить куки и сессию.
    """
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        n = _close_windows(proc.pid)
        log.info("закрываю окна браузера: %d", n)
    else:
        proc.terminate()
    try:
        proc.wait(20)
        return
    except subprocess.TimeoutExpired:
        pass
    log.warning("браузер не закрылся за 20 с — завершаю принудительно (возможно, на странице вопрос «Покинуть сайт?»)")
    if sys.platform == "win32":
        r = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        log.info("taskkill /F: код %s %s", r.returncode, (r.stdout or r.stderr).decode("cp866", "replace").strip())
    proc.kill()
    proc.wait(5)


def _quiet_connection_resets(loop: asyncio.AbstractEventLoop) -> None:
    """На Windows обрыв соединения браузером при закрытии — норма, не пишем его как ошибку."""
    default = loop.get_exception_handler()

    def handler(lp, ctx):
        if isinstance(ctx.get("exception"), ConnectionResetError | ConnectionAbortedError | BrokenPipeError):
            return
        if default:
            default(lp, ctx)
        else:
            lp.default_exception_handler(ctx)

    loop.set_exception_handler(handler)


async def run(pid: str, confirm_country: bool) -> int:
    from .core.geo import lookup

    _quiet_connection_resets(asyncio.get_running_loop())
    cfg = settings.load()
    p = profiles.get(pid)
    state = lambda **kw: runstate.write(pid, **kw)  # noqa: E731
    state(status="starting", runner_pid=os.getpid(), app_version=__version__, error=None, name=p.data["name"],
          started=time.time(), chrome_pid=None, needs_confirmation=None)

    version = cfg["engine_version"]
    exe = engine.find_exe(version)
    if not exe:
        raise Stop(EXIT_ERROR, "error", f"движок {version} не установлен")
    last = p.data.get("engine", {}).get("last_version")
    if last and engine.parse_version(version) < engine.parse_version(last):
        raise Stop(EXIT_ERROR, "error", f"профиль открывался в движке {last}, а сейчас установлен {version}; "
                                         f"запуск в более старой версии может испортить профиль")

    try:
        up = parse_proxy(p.data.get("proxy") or "")
    except ValueError as e:
        raise Stop(EXIT_PROXY, "error", f"строка прокси: {e}") from e
    if up is None:
        raise Stop(EXIT_PROXY, "error", "у профиля не задан прокси — без него браузер покажет реальный IP")

    fwd = Forwarder(up)
    await fwd.start()
    try:
        # ---- проверка прокси
        state(status="checking", proxy=up.masked())
        try:
            ip = await external_ip(fwd)
        except ProxyError as e:
            raise Stop(EXIT_PROXY, "error", f"прокси не работает: {e}") from e
        geo = await asyncio.to_thread(lookup, ip, fwd.port)
        prev = iphistory.last(p.dir)
        if (prev and prev.get("country") and geo.get("country") and prev["country"] != geo["country"]
                and not confirm_country):
            raise Stop(EXIT_CONFIRM, "needs_confirmation",
                       f"страна IP сменилась: {prev['country']} → {geo['country']} ({ip})",
                       needs_confirmation={"from": prev["country"], "to": geo["country"], "ip": ip})
        iphistory.record(p.dir, ip, geo, "launch")

        # ---- подготовка профиля
        data_dir = p.dir / "data"
        data_dir.mkdir(exist_ok=True)
        portability.clear_locks(data_dir)
        prefs.apply(data_dir)
        key_state = portability.restore_key(p.dir) if sys.platform == "win32" else "skipped"
        if key_state == "no-backup":
            log.warning("ключ кук не расшифровывается, резервной копии нет — сайты попросят войти заново")
        fp = p.data["fingerprint"]
        tz = geo.get("timezone") if fp.get("timezone", "auto") == "auto" else fp["timezone"]
        cache = profiles.cache_dir(pid)
        cache.mkdir(parents=True, exist_ok=True)
        args = engine.build_args(data_dir=data_dir, cache_dir=cache, fingerprint=fp, proxy_port=fwd.port,
                                 timezone=tz, urls=p.data.get("start_urls"))

        # ---- запуск
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.Popen([str(exe), *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=flags)
        profiles.touch_machine(p, version)
        profiles.append_launch_log(p, f"запуск\t{ip}\t{geo.get('country')}\t{geo.get('city')}\t"
                                      f"ключ:{key_state}\tдвижок:{version}")
        state(status="running", chrome_pid=proc.pid, ip=ip, geo=geo, timezone=tz, key_state=key_state,
              engine_version=version, forwarder_port=fwd.port, last_ip_check=time.time())
        log.info("профиль %s запущен: %s %s", p.slug, ip, geo.get("country"))

        # ---- работа
        interval = max(1, int(cfg["monitor_interval_min"])) * 60
        next_check = time.monotonic() + interval
        next_beat = time.monotonic() + runstate.HEARTBEAT_S
        key_saved = False
        stop_reason = "браузер закрыт"
        while proc.poll() is None:
            await asyncio.sleep(1)
            if runstate.stop_file(pid).exists():
                runstate.stop_file(pid).unlink(missing_ok=True)
                state(status="stopping")
                log.info("профиль %s: получена команда остановки", p.slug)
                t0 = time.monotonic()
                closing = asyncio.create_task(asyncio.to_thread(_close_browser, proc))
                while not closing.done():  # пока закрываем — продолжаем сообщать, что живы
                    await asyncio.wait({closing}, timeout=runstate.HEARTBEAT_S)
                    state()
                await closing
                stop_reason = f"остановлен из лаунчера (браузер закрылся за {time.monotonic() - t0:.0f} с)"
                break
            if not key_saved and sys.platform == "win32":
                key_saved = (p.dir / portability.KEY_FILE).exists() or portability.save_key(p.dir)
            now = time.monotonic()
            if now >= next_check:
                next_check = now + interval
                await monitor_ip(p, pid, fwd, lookup, state)
            if now >= next_beat:
                next_beat = now + runstate.HEARTBEAT_S
                state()
        if sys.platform == "win32" and not key_saved:
            portability.save_key(p.dir)
        profiles.append_launch_log(p, stop_reason)
        log.info("профиль %s: %s", p.slug, stop_reason)
        return EXIT_OK
    finally:
        await fwd.stop()


async def monitor_ip(p, pid: str, fwd: Forwarder, lookup, state) -> None:
    try:
        ip = await external_ip(fwd)
    except ProxyError as e:
        state(warning=f"прокси перестал отвечать: {e}", last_ip_check=time.time())
        return
    st = runstate.read_raw(pid) or {}
    if ip == st.get("ip"):
        state(warning=None, last_ip_check=time.time())
        return
    geo = await asyncio.to_thread(lookup, ip, fwd.port)
    rec = iphistory.record(p.dir, ip, geo, "monitor")
    warning = None
    if rec["country_changed"]:
        warning = f"страна IP сменилась во время работы: {rec['previous']['country']} → {geo.get('country')} ({ip})"
    state(ip=ip, geo=geo, warning=warning, last_ip_check=time.time())


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("использование: python -m xlauncher.runner <profile_id> [--confirm-country]")
        return EXIT_ERROR
    pid = args[0]
    paths.ensure()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(paths.logs_dir() / "runner.log", encoding="utf-8")])
    if runstate.is_active(pid) and (runstate.read_raw(pid) or {}).get("runner_pid") != os.getpid():
        log.warning("профиль %s уже запущен", pid)
        return EXIT_ERROR
    runstate.stop_file(pid).unlink(missing_ok=True)
    try:
        code = asyncio.run(run(pid, "--confirm-country" in sys.argv))
        runstate.clear(pid)
        return code
    except Stop as s:
        log.warning("профиль %s: %s", pid, s.error or s.status)
        runstate.write(pid, status=s.status, error=s.error, chrome_pid=None, **s.extra)
        return s.code
    except Exception as e:  # noqa: BLE001
        log.exception("раннер %s", pid)
        runstate.write(pid, status="error", error=f"{type(e).__name__}: {e}", chrome_pid=None)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
