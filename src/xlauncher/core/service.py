"""Операции над профилями для API: список со статусами, запуск/остановка раннеров, проверка прокси."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from .. import settings
from . import engine, iphistory, profiles, proxy, runstate


def _run_async(coro):
    return asyncio.run(coro)


def profile_view(p: profiles.Profile, duplicate: bool = False) -> dict:
    d = p.public()
    d["proxy_masked"] = _mask(d.get("proxy", ""))
    d.pop("proxy", None)
    d["state"] = runstate.read(p.id)
    d["last_ip"] = iphistory.last(p.dir)
    d["duplicate"] = duplicate
    return d


def _mask(s: str) -> str:
    try:
        up = proxy.parse_proxy(s)
        return up.masked() if up else ""
    except ValueError:
        return "(неверная строка)"


def list_profiles() -> dict:
    items, dups = profiles.load_all()
    dup_ids = set(dups)
    return {
        "profiles": [profile_view(p, p.id in dup_ids) for p in items],
        "duplicates": [{"id": k, "slugs": [p.slug for p in v]} for k, v in dups.items()],
    }


def get_profile(pid: str, with_proxy: bool = False) -> dict:
    p = profiles.get(pid)
    d = profile_view(p)
    if with_proxy:
        d["proxy"] = p.data.get("proxy", "")
    return d


def check_proxy(proxy_str: str) -> dict:
    return _run_async(proxy.check(proxy_str))


def create_profile(body: dict) -> dict:
    country = body.get("country")
    check = None
    if body.get("proxy") and not country:
        check = check_proxy(body["proxy"])
        if check.get("ok"):
            country = check["geo"].get("country")
    p = profiles.create(body.get("name") or "Профиль", group=body.get("group", ""), proxy=body.get("proxy", ""),
                        start_urls=body.get("start_urls") or [], notes=body.get("notes", ""), country=country,
                        locale=body.get("locale"))
    if check and check.get("ok"):
        iphistory.record(p.dir, check["ip"], check["geo"], "create")
    return {"profile": profile_view(p), "check": check}


def _runner_cmd(pid: str, confirm_country: bool) -> tuple[list[str], dict, Path | None]:
    exe = sys.executable
    if sys.platform == "win32" and exe.lower().endswith("python.exe"):
        w = Path(exe).with_name("pythonw.exe")
        if w.exists():
            exe = str(w)
    cmd = [exe, "-m", "xlauncher.runner", pid] + (["--confirm-country"] if confirm_country else [])
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[2])  # папка, где лежит пакет xlauncher
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return cmd, env, Path(src)


def start(pid: str, confirm_country: bool = False) -> dict:
    p = profiles.get(pid)  # проверяет существование и дубли
    if runstate.is_active(pid):
        return {"ok": False, "error": "профиль уже запущен"}
    version = settings.load()["engine_version"]
    if not engine.find_exe(version):
        engine.installer.start(version)
        return {"ok": False, "error": f"движок {version} скачивается — запусти профиль, когда загрузка закончится",
                "engine": engine.installer.state}
    runstate.clear(pid)
    cmd, env, cwd = _runner_cmd(pid, confirm_country)
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True,
                            start_new_session=sys.platform != "win32")
    runstate.write(pid, status="starting", runner_pid=proc.pid, name=p.data["name"], error=None)
    return {"ok": True, "runner_pid": proc.pid}


def stop(pid: str) -> dict:
    if not runstate.is_active(pid):
        runstate.clear(pid)
        return {"ok": True, "was_running": False}
    runstate.request_stop(pid)
    return {"ok": True, "was_running": True}


def dismiss(pid: str) -> dict:
    """Убирает с карточки ошибку прошлого запуска."""
    if not runstate.is_active(pid):
        runstate.clear(pid)
    return {"ok": True}


def engine_status() -> dict:
    version = settings.load()["engine_version"]
    return {"version": version, "installed": bool(engine.find_exe(version)),
            "installed_versions": engine.installed_versions(), "installer": engine.installer.state}


def engine_install() -> dict:
    version = settings.load()["engine_version"]
    if not engine.find_exe(version):
        engine.installer.start(version)
    return engine_status()
