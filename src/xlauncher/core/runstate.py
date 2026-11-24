"""Состояние раннеров: <корень>/run/profile-<id>.json.

Раннер — отдельный процесс на каждый запущенный профиль. Он пишет сюда статус и раз в
HEARTBEAT_S секунд обновляет heartbeat. Приложение только читает эти файлы и кладёт
рядом profile-<id>.stop, чтобы попросить раннер закрыть браузер. Так приложение можно
перезапускать (обновлять), не трогая открытые браузеры (ARCHITECTURE.md §2.1).
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from pathlib import Path

from .. import paths

PROTOCOL = 1
HEARTBEAT_S = 10
STALE_S = 45

# Статусы, при которых раннер работает.
ACTIVE = {"starting", "checking", "running", "stopping"}


def state_file(pid: str) -> Path:
    return paths.run_dir() / f"profile-{pid}.json"


def stop_file(pid: str) -> Path:
    return paths.run_dir() / f"profile-{pid}.stop"


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def write(pid: str, **fields) -> dict:
    paths.run_dir().mkdir(parents=True, exist_ok=True)
    cur = read_raw(pid) or {}
    cur.update(fields, protocol=PROTOCOL, heartbeat=time.time())
    paths.write_atomic(state_file(pid), json.dumps(cur, ensure_ascii=False))
    return cur


def read_raw(pid: str) -> dict | None:
    try:
        return json.loads(state_file(pid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read(pid: str) -> dict | None:
    """Состояние с поправкой на мёртвые раннеры: активный статус без живого процесса → crashed."""
    st = read_raw(pid)
    if not st:
        return None
    if st.get("status") in ACTIVE:
        fresh = time.time() - st.get("heartbeat", 0) < STALE_S
        if not (fresh and pid_alive(st.get("runner_pid"))):
            st["status"] = "crashed"
            st.setdefault("error", "раннер завершился аварийно")
    return st


def is_active(pid: str) -> bool:
    st = read(pid)
    return bool(st and st.get("status") in ACTIVE)


def clear(pid: str) -> None:
    state_file(pid).unlink(missing_ok=True)
    stop_file(pid).unlink(missing_ok=True)


def request_stop(pid: str) -> None:
    paths.run_dir().mkdir(parents=True, exist_ok=True)
    stop_file(pid).write_text(str(time.time()), encoding="utf-8")


def active_app_versions() -> set[str]:
    """Версии приложения, из которых сейчас работают раннеры — их нельзя удалять при очистке."""
    out = set()
    for f in paths.run_dir().glob("profile-*.json"):
        pid = f.stem.removeprefix("profile-")
        st = read(pid)
        if st and st.get("status") in ACTIVE and st.get("app_version"):
            out.add(st["app_version"])
    return out
