"""Локальный HTTP-сервер: API и страница интерфейса. Слушает только 127.0.0.1."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__, paths, settings
from .core import iphistory, profiles, service

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
        }

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
        return service.start(pid, bool(body.get("confirm_country")))

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
