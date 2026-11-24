"""Операции над профилями для API: список со статусами, запуск/остановка раннеров, проверка прокси."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from .. import settings
from . import engine, fingerprint, iphistory, profiles, proxy, runstate, uniqueness


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
    fp, unique = uniqueness.pick(fingerprint.generate(country, body.get("locale")))
    p = profiles.create(body.get("name") or "Профиль", group=body.get("group", ""), proxy=body.get("proxy", ""),
                        start_urls=body.get("start_urls") or [], notes=body.get("notes", ""), fp=fp)
    if check and check.get("ok"):
        iphistory.record(p.dir, check["ip"], check["geo"], "create")
    return {"profile": profile_view(p), "check": check, "uniqueness": unique}


def clone_profile(pid: str, name: str | None) -> dict:
    p = profiles.clone(pid, name)
    report = uniqueness.ensure_unique(p)
    return {"profile": profile_view(p), "uniqueness": report}


def new_identity(slug: str) -> dict:
    p = profiles.new_identity(profiles.get_by_slug(slug))
    report = uniqueness.ensure_unique(p)
    return {"profile": profile_view(p), "uniqueness": report}


def audit_start() -> dict:
    uniqueness.auditor.start()
    return uniqueness.auditor.state


def _runner_cmd(pid: str, confirm_country: bool, agent: bool = False) -> tuple[list[str], dict, Path | None]:
    exe = sys.executable
    if sys.platform == "win32":
        for name in ("xlauncher-runner.exe", "pythonw.exe"):  # своё имя процесса в диспетчере задач
            w = Path(exe).with_name(name)
            if w.exists():
                exe = str(w)
                break
    cmd = [exe, "-m", "xlauncher.runner", pid] + (["--confirm-country"] if confirm_country else []) \
        + (["--agent"] if agent else [])
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[2])  # папка, где лежит пакет xlauncher
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return cmd, env, Path(src)


def start(pid: str, confirm_country: bool = False, agent: bool = False) -> dict:
    p = profiles.get(pid)  # проверяет существование и дубли
    if runstate.is_active(pid):
        return {"ok": False, "error": "профиль уже запущен"}
    version = settings.load()["engine_version"]
    if not engine.find_exe(version):
        engine.installer.start(version)
        return {"ok": False, "error": f"движок {version} скачивается — запусти профиль, когда загрузка закончится",
                "engine": engine.installer.state}
    runstate.clear(pid)
    cmd, env, cwd = _runner_cmd(pid, confirm_country, agent)
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


def launch_log(pid: str, limit: int = 60) -> list[dict]:
    p = profiles.get(pid)
    try:
        lines = (p.dir / "launch.log").read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        parts = ln.split("\t")
        out.append({"time": parts[0], "event": parts[1] if len(parts) > 1 else "", "details": parts[2:]})
    return out


def open_folder(path: Path) -> dict:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606
        return {"ok": True}
    return {"ok": False, "error": "открыть папку можно только на Windows", "path": str(path)}


# ---------------------------------------------------------------- браузер для агентов

BROWSER_ACTIONS = {"tabs", "new_tab", "activate_tab", "close_tab", "navigate", "snapshot", "click", "type", "press_key",
                   "scroll", "screenshot", "wait"}


def browser_action(pid: str, action: str, args: dict) -> dict:
    """Действие агента в браузере профиля. Только для профиля, запущенного с доступом агента."""
    import base64

    from . import browser

    if action not in BROWSER_ACTIONS:
        raise ValueError(f"неизвестное действие {action}")
    p = profiles.get(pid)
    st = runstate.read(pid) or {}
    if st.get("status") != "running":
        raise ValueError("профиль не запущен")
    port = st.get("cdp_port")
    if not port:
        raise ValueError("профиль запущен без доступа агента — останови его и запусти через агента")
    tab = args.get("tab")
    try:
        if action == "tabs":
            res: dict = {"tabs": browser.tabs(port)}
        elif action == "new_tab":
            res = browser.new_tab(port, args.get("url") or "about:blank")
        elif action == "activate_tab":
            browser.activate_tab(port, args["tab"])
            res = {"ok": True}
        elif action == "close_tab":
            browser.close_tab(port, args["tab"])
            res = {"ok": True}
        elif action == "navigate":
            res = _run_async(browser.navigate(port, args["url"], tab))
        elif action == "snapshot":
            snap = _run_async(browser.snapshot(port, tab, int(args.get("text_limit") or 3000)))
            res = {"text": browser.format_snapshot(snap), "url": snap["url"], "title": snap["title"]}
        elif action == "click":
            res = _run_async(browser.click(port, args["ref"], tab))
        elif action == "type":
            res = _run_async(browser.type_text(port, args["ref"], args["text"], bool(args.get("submit")),
                                               args.get("clear", True), tab))
        elif action == "press_key":
            res = _run_async(browser.press_key(port, args["key"], tab))
        elif action == "scroll":
            res = _run_async(browser.scroll(port, int(args.get("dy") or 600), tab))
        elif action == "screenshot":
            res = {"jpeg_base64": base64.b64encode(_run_async(browser.screenshot(port, tab))).decode()}
        else:
            res = _run_async(browser.wait_for(port, args.get("text"), float(args.get("seconds") or 2), tab))
    except browser.BrowserError as e:
        raise ValueError(str(e)) from e
    except KeyError as e:
        raise ValueError(f"не хватает параметра {e.args[0]}") from e
    detail = {k: (v if k != "text" else f"{len(str(v))} симв.") for k, v in args.items() if k != "tab"}
    profiles.append_launch_log(p, f"агент: {action}\t{json.dumps(detail, ensure_ascii=False)[:200]}")
    return res
