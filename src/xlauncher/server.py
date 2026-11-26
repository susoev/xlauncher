"""Локальный HTTP-сервер: API и страница интерфейса. Слушает только 127.0.0.1."""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__, paths, settings
from .core import firewall, iphistory, profiles, runstate, service, tailscale

if TYPE_CHECKING:
    from .updater import Updater


def create_app(updater: "Updater | None") -> FastAPI:
    app = FastAPI(title="xlauncher", version=__version__, docs_url=None, redoc_url=None)
    app.state.last_ui_ping = None

    @app.exception_handler(KeyError)
    async def not_found(_: Request, e: KeyError):
        return JSONResponse({"error": f"профиль не найден: {e.args[0] if e.args else ''}"}, status_code=404)

    @app.exception_handler(profiles.DuplicateError)
    async def duplicate(_: Request, e: profiles.DuplicateError):
        return JSONResponse({"error": str(e), "slugs": e.slugs}, status_code=409)

    @app.exception_handler(ValueError)
    async def bad_request(_: Request, e: ValueError):
        return JSONResponse({"error": str(e)}, status_code=400)

    from .core import move

    @app.exception_handler(move.MoveError)
    async def move_error(_: Request, e: move.MoveError):
        return JSONResponse({"error": str(e)}, status_code=400)

    # ---------------------------------------------------------- служебное

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": __version__}

    @app.get("/api/status")
    def status() -> dict:
        return {
            "version": __version__,
            "pid": os.getpid(),
            "root": str(paths.root()),
            "updater": updater.status() if updater else {"state": "выключено (запуск из исходников)"},
            "engine": service.engine_status(),
            "audit": service.uniqueness.auditor.state,
            "bootstrap_version": updater.bootstrap_version() if updater else None,
            "host": __import__("platform").node(),
            "tailscale": tailscale.current(),
            "firewall": _firewall_status(),
            "move": {"export": (_ms := move.load_state() or {}).get("phase"), "pull": bool(_ms.get("pull")),
                     "pending": len(move.pending_imports())},
        }

    @app.get("/api/tailscale")
    def tailscale_status() -> dict:
        return tailscale.refresh()

    @app.post("/api/tailscale/{what}")
    def tailscale_action(what: str) -> dict:
        return tailscale.action(what)

    @app.post("/api/ui-ping")
    def ui_ping() -> dict:
        import time

        app.state.last_ui_ping = time.time()
        return {"ok": True}

    @app.post("/api/update/check")
    def update_check() -> dict:
        if updater:
            updater.check_now()
        return {"ok": bool(updater)}

    @app.post("/api/engine/install")
    def engine_install() -> dict:
        return service.engine_install()

    # ---------------------------------------------------------- профили

    @app.get("/api/profiles")
    def list_profiles() -> dict:
        return service.list_profiles()

    @app.post("/api/profiles")
    def create_profile(body: dict = Body(...)) -> dict:
        if not (body.get("name") or "").strip():
            raise ValueError("укажи имя профиля")
        return service.create_profile(body)

    @app.get("/api/profiles/{pid}")
    def get_profile(pid: str, with_proxy: bool = False) -> dict:
        return service.get_profile(pid, with_proxy)

    @app.patch("/api/profiles/{pid}")
    def update_profile(pid: str, body: dict = Body(...)) -> dict:
        return service.profile_view(profiles.update(pid, body))

    @app.delete("/api/profiles/{pid}")
    def delete_profile(pid: str) -> dict:
        if service.runstate.is_active(pid):
            raise HTTPException(409, "сначала останови профиль")
        return {"ok": True, "backup": str(profiles.delete(pid))}

    @app.post("/api/profiles/{pid}/clone")
    def clone_profile(pid: str, body: dict = Body(default={})) -> dict:
        return service.clone_profile(pid, body.get("name"))

    @app.post("/api/profiles/{pid}/start")
    def start(pid: str, body: dict = Body(default={})) -> dict:
        return service.start(pid, bool(body.get("confirm_country")), bool(body.get("agent")))

    @app.post("/api/profiles/{pid}/browser/{action}")
    def browser_action(pid: str, action: str, body: dict = Body(default={})) -> dict:
        return service.browser_action(pid, action, body)

    @app.post("/api/profiles/{pid}/warmup")
    def warmup_start(pid: str, body: dict = Body(default={})) -> dict:
        profiles.get(pid)
        return service.warmup_start(pid, body)

    @app.post("/api/profiles/{pid}/day-routine")
    def day_routine(pid: str, body: dict = Body(default={})) -> dict:
        profiles.get(pid)
        return service.day_routine_start(pid, body)

    @app.post("/api/profiles/{pid}/warmup/stop")
    def warmup_stop(pid: str) -> dict:
        return service.warmup_stop(pid)

    @app.get("/api/warmup/sites")
    def warmup_sites() -> dict:
        return {"sites": service.warmup_sites()}

    @app.post("/api/profiles/{pid}/stop")
    def stop(pid: str) -> dict:
        return service.stop(pid)

    @app.post("/api/profiles/{pid}/dismiss")
    def dismiss(pid: str) -> dict:
        return service.dismiss(pid)

    @app.get("/api/profiles/{pid}/ip-history")
    def ip_history(pid: str) -> dict:
        return {"history": iphistory.read(profiles.get(pid).dir)}

    @app.get("/api/profiles/{pid}/launch-log")
    def launch_log(pid: str) -> dict:
        return {"log": service.launch_log(pid)}

    @app.post("/api/profiles/{pid}/open-folder")
    def open_profile_folder(pid: str) -> dict:
        return service.open_folder(profiles.get(pid).dir)

    @app.post("/api/open-root")
    def open_root() -> dict:
        return service.open_folder(paths.root())

    @app.get("/api/settings")
    def get_settings() -> dict:
        return settings.load()

    @app.patch("/api/settings")
    def patch_settings(body: dict = Body(...)) -> dict:
        allowed = {"monitor_interval_min"}
        bad = set(body) - allowed
        if bad:
            raise ValueError(f"нельзя менять: {', '.join(sorted(bad))}")
        if "monitor_interval_min" in body:
            v = int(body["monitor_interval_min"])
            if not 1 <= v <= 120:
                raise ValueError("интервал проверки IP — от 1 до 120 минут")
            body["monitor_interval_min"] = v
        settings.save(body)
        return settings.load()

    def _firewall_status() -> dict | None:
        """Брандмауэр — только когда удалённый доступ включён (иначе он не важен)."""
        from .core import remote

        cfg = remote.config()
        if not cfg["enabled"] or not firewall.supported():
            return None
        fw = firewall.current()
        if fw.get("port") != cfg["port"]:
            fw = {"supported": True, "ok": None, "checking": True}
        return fw

    # ---------------------------------------------------------- переезд
    # Только локальный API (127.0.0.1): удалённый MCP сюда не ходит, MCP-инструментов переезда нет —
    # ключ даёт доступ ко всем аккаунтам, а очистка удаляет данные.

    @app.get("/api/move/status")
    def move_status() -> dict:
        from .core import pull

        return {"job": move.job.snapshot(), "export": move.export_summary(), "relay": move.relay_setting(),
                "pull": pull.pull_public(), "paired": bool(pull.pair()),
                "pending_confirms": len(move.pending_imports()), "tailscale": tailscale.current()}

    @app.get("/api/move/estimate")
    def move_estimate() -> dict:
        items, _ = profiles.load_all()
        running = [p.data.get("name") or p.id for p in items if runstate.is_active(p.id)]
        return {**move.estimate(), "running": running}

    @app.post("/api/move/relay")
    def move_relay(body: dict = Body(...)) -> dict:
        return move.set_relay(str(body.get("relay") or ""))

    @app.post("/api/move/stop-all")
    def move_stop_all() -> dict:
        items, _ = profiles.load_all()
        stopped = [p.id for p in items if runstate.is_active(p.id) and service.stop(p.id)["was_running"]]
        for _ in range(150):  # до 30 с: раннеры закрывают браузер и сохраняют профиль
            if not any(runstate.is_active(pid) for pid in stopped):
                break
            time.sleep(0.2)
        return {"ok": True, "stopped": len(stopped),
                "still_running": [pid for pid in stopped if runstate.is_active(pid)]}

    @app.post("/api/move/export")
    def move_export() -> dict:
        relay = move.relay_setting()
        if not relay:
            raise move.MoveError("укажите адрес сервера переезда")
        return move.job.start("export", move.export, relay)

    @app.post("/api/move/export/resume")
    def move_export_resume() -> dict:
        return move.job.start("upload", move.upload)

    @app.post("/api/move/export/cancel")
    def move_export_cancel() -> dict:
        move.job.cancel()
        return move.cancel_export()

    @app.get("/api/move/key")
    def move_key() -> dict:
        st = move.load_state()
        if not st or st.get("phase") != "sent":
            raise move.MoveError("нет отправленного переезда")
        return {"key": st["key"], "key_id": move.key_id(st["key"])}

    @app.get("/api/move/remote")
    def move_remote() -> dict:
        return {"status": move.remote_status()}

    @app.post("/api/move/import/inspect")
    def move_inspect(body: dict = Body(...)) -> dict:
        return move.inspect(str(body.get("key") or ""))

    @app.post("/api/move/import")
    def move_import(body: dict = Body(...)) -> dict:
        key = str(body.get("key") or "")
        move.parse_key(key)
        conflicts = {str(k): str(v) for k, v in (body.get("conflicts") or {}).items()}
        return move.job.start("import", move.receive, key, conflicts=conflicts)

    @app.post("/api/move/import/cancel")
    def move_import_cancel(body: dict = Body(...)) -> dict:
        move.job.cancel()
        move.cancel_import(str(body.get("key") or ""))
        return {"ok": True}

    @app.post("/api/move/finish")
    def move_finish(body: dict = Body(...)) -> dict:
        return move.finish(str(body.get("confirm") or ""))

    # «Забрать сюда»: связка машин
    @app.get("/api/move/pair")
    def move_pair() -> dict:
        from .core import pull

        p = pull.pair()
        return {"paired": bool(p), "code": p.code if p else None, "relay": p.relay if p else move.relay_setting()}

    @app.post("/api/move/pair")
    def move_pair_set(body: dict = Body(...)) -> dict:
        from .core import pull

        if body.get("new"):
            relay = str(body.get("relay") or move.relay_setting() or "")
            if not relay:
                raise move.MoveError("укажите адрес сервера переезда")
            code = pull.new_pair_code(relay)
        else:
            code = str(body.get("code") or "")
        pull.set_pair(code)
        return move_pair()

    @app.post("/api/move/pair/clear")
    def move_pair_clear() -> dict:
        from .core import pull

        return pull.clear_pair()

    @app.get("/api/move/devices")
    def move_devices() -> dict:
        from .core import pull

        return pull.devices()

    @app.post("/api/move/pull")
    def move_pull(body: dict = Body(...)) -> dict:
        from .core import pull

        return pull.start_pull(str(body.get("target") or ""))

    @app.post("/api/move/pull/cancel")
    def move_pull_cancel() -> dict:
        from .core import pull

        return pull.cancel_pull()

    @app.post("/api/move/pull/clear")
    def move_pull_clear() -> dict:
        from .core import pull

        st = pull.load_pull()
        if st and st.get("phase") in ("done", "error", "cancelled"):
            pull.pull_path().unlink(missing_ok=True)
        return {"ok": True}

    @app.get("/api/remote")
    def remote_status() -> dict:
        from .core import remote

        return {**remote.server.status(), "autostart": service.autostart_get(), "firewall": _firewall_status()}

    @app.post("/api/remote/firewall/{what}")
    def remote_firewall(what: str) -> dict:
        """check — проверить заново; fix — правило по порту и снятие запретов (запрос UAC)."""
        from .core import remote

        port = int(remote.config()["port"])
        if what == "check":
            return {"ok": True, "firewall": firewall.check(port)}
        if what == "fix":
            return firewall.fix(port)
        raise ValueError("неизвестное действие")

    @app.post("/api/remote")
    def remote_update(body: dict = Body(...)) -> dict:
        from .core import remote

        vals = {}
        if "enabled" in body:
            vals["enabled"] = bool(body["enabled"])
            if vals["enabled"] and not remote.config()["token"]:
                vals["token"] = remote.new_token()
        if "port" in body:
            port = int(body["port"])
            if not 1024 <= port <= 65535:
                raise ValueError("порт — от 1024 до 65535")
            vals["port"] = port
        if body.get("new_token"):
            vals["token"] = remote.new_token()
        if vals:
            remote.save(vals)
            remote.server.apply()
            if remote.config()["enabled"]:
                firewall.check(int(remote.config()["port"]))
        if "autostart" in body:
            service.autostart_set(bool(body["autostart"]))
        return {**remote.server.status(), "autostart": service.autostart_get(), "firewall": _firewall_status()}

    @app.post("/api/proxy/check")
    def check_proxy(body: dict = Body(...)) -> dict:
        return service.check_proxy(body.get("proxy", ""))

    @app.post("/api/duplicates/{slug}/new-identity")
    def dup_new_identity(slug: str) -> dict:
        return service.new_identity(slug)

    @app.post("/api/fingerprints/audit")
    def audit() -> dict:
        return service.audit_start()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        from .ui import INDEX_HTML

        return INDEX_HTML

    return app
