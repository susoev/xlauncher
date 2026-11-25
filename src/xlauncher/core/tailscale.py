"""Состояние Tailscale на этом компьютере: нужен для переезда, удалённого доступа агентов и прокси в tailnet.

status() -> {"state": missing | stopped | needs_login | offline | running, "host", "ip", "auth_url", "error"}
current() — то же из кеша (обновляется в фоне не чаще раза в 60 с), чтобы /api/status не ждал tailscale.exe.

Лаунчер без Tailscale работает полностью, кроме переезда и удалённого доступа.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

DOWNLOAD_URL = "https://tailscale.com/download/windows"
REFRESH_S = 60
TIMEOUT_S = 5

_cache: dict = {"state": "checking", "checked": 0.0}
_lock = threading.Lock()
_busy = False


def find_exe() -> Path | None:
    """tailscale.exe (CLI): переменная XL_TAILSCALE_EXE (тесты), Program Files, реестр, PATH."""
    env = os.environ.get("XL_TAILSCALE_EXE")
    if env:
        return Path(env) if Path(env).exists() else None
    cands: list[Path] = []
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                cands.append(Path(base) / "Tailscale" / "tailscale.exe")
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tailscale IPN") as k:
                d = winreg.QueryValueEx(k, "InstallDir")[0]
                cands.append(Path(d) / "tailscale.exe")
        except OSError:
            pass
    which = shutil.which("tailscale")
    if which:
        cands.append(Path(which))
    return next((c for c in cands if c.exists()), None)


def gui_exe() -> Path | None:
    """tailscale-ipn.exe — окно и значок в трее (рядом с CLI)."""
    exe = find_exe()
    if not exe:
        return None
    g = exe.with_name("tailscale-ipn.exe")
    return g if g.exists() else None


def _service_running() -> bool | None:
    """Служба Tailscale в Windows: True/False, None — не удалось узнать (не Windows, нет pywin32)."""
    if sys.platform != "win32":
        return None
    try:
        import win32service
        import win32serviceutil

        return win32serviceutil.QueryServiceStatus("Tailscale")[1] == win32service.SERVICE_RUNNING
    except Exception:  # noqa: BLE001
        return None


def _parse(d: dict) -> dict:
    backend = d.get("BackendState") or ""
    me = d.get("Self") or {}
    ips = [ip for ip in (me.get("TailscaleIPs") or d.get("TailscaleIPs") or []) if ":" not in ip]
    base = {"host": me.get("HostName"), "ip": ips[0] if ips else None, "auth_url": d.get("AuthURL") or None}
    if backend == "Running" and me.get("Online", True):
        return {"state": "running", **base}
    if backend in ("NeedsLogin", "NeedsMachineAuth"):
        return {"state": "needs_login", **base}
    if backend == "Stopped":
        return {"state": "stopped", **base}
    return {"state": "offline", **base, "error": backend or None}


def status() -> dict:
    exe = find_exe()
    if not exe:
        return {"state": "missing"}
    if _service_running() is False:
        return {"state": "stopped"}
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        r = subprocess.run([str(exe), "status", "--json"], capture_output=True, timeout=TIMEOUT_S,
                           creationflags=flags)
        d = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    except subprocess.TimeoutExpired:
        return {"state": "offline", "error": "tailscale не ответил за 5 с"}
    except (OSError, ValueError) as e:
        # служба не отвечает: CLI пишет ошибку вместо JSON
        return {"state": "stopped", "error": str(e)[:200]}
    return _parse(d)


def refresh() -> dict:
    global _busy
    try:
        res = status()
    except Exception as e:  # noqa: BLE001
        res = {"state": "offline", "error": f"{type(e).__name__}: {e}"}
    with _lock:
        _cache.clear()
        _cache.update(res, checked=time.time())
        _busy = False
    return dict(_cache)


def current() -> dict:
    """Кешированное состояние; устаревшее — обновляется в фоне."""
    global _busy
    with _lock:
        stale = time.time() - _cache.get("checked", 0) > REFRESH_S
        if stale and not _busy:
            _busy = True
            threading.Thread(target=refresh, name="tailscale-status", daemon=True).start()
        return dict(_cache)


def action(what: str) -> dict:
    """Кнопки баннера: download — страница установщика; start/open — окно Tailscale (или ссылка входа); recheck."""
    import webbrowser

    if what == "download":
        webbrowser.open(DOWNLOAD_URL)
        return {"ok": True}
    if what == "recheck":
        return {"ok": True, "tailscale": refresh()}
    if what in ("start", "open"):
        st = current()
        if what == "open" and st.get("auth_url"):
            webbrowser.open(st["auth_url"])
            return {"ok": True}
        g = gui_exe()
        if not g:
            return {"ok": False, "error": "не нашёл tailscale-ipn.exe — открой Tailscale из меню «Пуск»"}
        flags = subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
        subprocess.Popen([str(g)], creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return {"ok": True}
    return {"ok": False, "error": "неизвестное действие"}
