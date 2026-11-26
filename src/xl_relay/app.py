"""HTTP API релея (FastAPI). Описание — в __init__.py и ARCHITECTURE.md («Переезд»)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import VERSION

log = logging.getLogger("xl_relay")

HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
CHUNK_MAX = 9 * 1024 * 1024          # 8 МиБ открытого текста + тег GCM с запасом
MAX_CHUNKS = 100_000
DEVICE_MAX = 8 * 1024                # heartbeat машины
MESSAGE_MAX = 64 * 1024              # сообщение в почтовый ящик
INBOX_MAX = 200
DEVICES_MAX = 50


def _store() -> Path:
    return Path(os.environ.get("XL_RELAY_STORE") or Path.cwd() / "store")


def _min_free() -> int:
    return int(float(os.environ.get("XL_RELAY_MIN_FREE_GB", "5")) * 1024 ** 3)


def _check(value: str, rx: re.Pattern, what: str) -> str:
    if not rx.match(value or ""):
        raise HTTPException(400, f"неверный {what}")
    return value


def _write_json(p: Path, data: dict) -> None:
    tmp = p.with_name(p.name + f".{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _hash_token(token: str) -> str:
    try:
        raw = bytes.fromhex(token)
    except (TypeError, ValueError):
        raise HTTPException(400, "неверный токен") from None
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- пакеты


def _pkg(pid: str) -> Path:
    return _store() / "packages" / _check(pid, HEX32, "id пакета")


def _tomb(pid: str) -> Path:
    return _store() / "packages" / f"{pid}.done"


def _chunk(d: Path, n: int) -> Path:
    return d / "chunks" / f"{n:06d}.bin"


def _received(d: Path) -> list[int]:
    out = []
    for f in (d / "chunks").glob("*.bin"):
        try:
            out.append(int(f.stem))
        except ValueError:
            pass
    return sorted(out)


def _meta(pid: str) -> tuple[Path, dict]:
    d = _pkg(pid)
    if not (d / "meta.json").exists():
        t = _tomb(pid)
        if t.exists():
            raise HTTPException(410, _read_json(t))
        raise HTTPException(404, "пакет не найден")
    return d, _read_json(d / "meta.json")


_locks: dict[str, asyncio.Lock] = {}


def _lock(pid: str) -> asyncio.Lock:
    return _locks.setdefault(pid, asyncio.Lock())


def _public(meta: dict, d: Path) -> dict:
    out = {k: v for k, v in meta.items() if k != "delete_hash"}
    out["received"] = _received(d)
    out["complete"] = not (d / ".partial").exists()
    return out


def _remove(pid: str, d: Path, status: str) -> None:
    shutil.rmtree(d)
    _write_json(_tomb(pid), {"status": status, "time": time.time()})


# ---------------------------------------------------------------- связка машин


def _group(gid: str) -> Path:
    return _store() / "groups" / _check(gid, HEX32, "id связки")


def _dev(dev: str) -> str:
    return _check(dev, HEX32, "id машины")


async def _json_body(request: Request, limit: int) -> dict:
    raw = await request.body()
    if len(raw) > limit:
        raise HTTPException(413, "слишком большое тело")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "тело — не JSON") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "тело — не объект")
    return data


def create_app() -> FastAPI:
    app = FastAPI(title="xl-relay", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        resp = await call_next(request)
        # без токенов и тел: путь с укороченными id
        path = re.sub(r"[0-9a-f]{32}", lambda m: m.group(0)[:8], request.url.path)
        log.info("%s %s %s %s", request.method, path, request.headers.get("content-length", "-"), resp.status_code)
        return resp

    @app.get("/v1/health")
    def health() -> dict:
        _store().mkdir(parents=True, exist_ok=True)
        return {"ok": True, "free_bytes": shutil.disk_usage(_store()).free, "version": VERSION}

    # ---------------------------------------------------------------- пакеты

    @app.post("/v1/packages/{pid}", status_code=201)
    def create(pid: str, body: dict = Body(...)) -> dict:
        d = _pkg(pid)
        if d.exists() or _tomb(pid).exists():
            raise HTTPException(409, "пакет уже есть")
        try:
            chunks, size = int(body["chunks"]), int(body["cipher_bytes"])
            meta = {"chunks": chunks, "cipher_bytes": size,
                    "cipher_sha256": _check(body["cipher_sha256"], HEX64, "cipher_sha256"),
                    "delete_hash": _check(body["delete_hash"], HEX64, "delete_hash"),
                    "source_host": str(body.get("source_host") or "")[:64],
                    "profiles": int(body.get("profiles") or 0),
                    "created": str(body.get("created") or "")[:40]}
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "нужны chunks, cipher_bytes, cipher_sha256, delete_hash") from None
        if not 1 <= chunks <= MAX_CHUNKS or not 0 < size <= chunks * CHUNK_MAX:
            raise HTTPException(400, "неверный размер пакета")
        _store().mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(_store()).free < size + _min_free():
            raise HTTPException(507, "на сервере не хватает места")
        (d / "chunks").mkdir(parents=True)
        (d / ".partial").touch()
        meta.update(uploaded=time.time(), completed=None, chunk_sha256={}, downloads=0, last_download=None)
        _write_json(d / "meta.json", meta)
        return {"ok": True}

    @app.get("/v1/packages/{pid}")
    def status(pid: str) -> dict:
        d, meta = _meta(pid)
        return _public(meta, d)

    @app.put("/v1/packages/{pid}/chunks/{n}")
    async def put_chunk(pid: str, n: int, request: Request) -> dict:
        d, meta = _meta(pid)
        if not 1 <= n <= meta["chunks"]:
            raise HTTPException(400, "неверный номер фрагмента")
        want = _check((request.headers.get("x-chunk-sha256") or "").lower(), HEX64, "X-Chunk-Sha256")
        if not (d / ".partial").exists():
            raise HTTPException(409, "пакет уже завершён")
        tmp = d / "chunks" / f"{n:06d}.{secrets.token_hex(4)}.tmp"
        h, size = hashlib.sha256(), 0
        try:
            with open(tmp, "wb") as f:
                async for part in request.stream():
                    size += len(part)
                    if size > CHUNK_MAX:
                        raise HTTPException(413, "фрагмент больше 9 МиБ")
                    h.update(part)
                    f.write(part)
            if not hmac.compare_digest(h.hexdigest(), want):
                raise HTTPException(400, "sha256 фрагмента не совпал")
            async with _lock(pid):
                if not (d / "meta.json").exists():
                    raise HTTPException(404, "пакет удалён")
                os.replace(tmp, _chunk(d, n))
                meta = _read_json(d / "meta.json")
                meta["chunk_sha256"][str(n)] = want
                _write_json(d / "meta.json", meta)
        finally:
            tmp.unlink(missing_ok=True)
        return {"ok": True, "n": n, "bytes": size}

    @app.post("/v1/packages/{pid}/complete")
    async def complete(pid: str) -> dict:
        async with _lock(pid):
            d, meta = _meta(pid)
            if (d / ".partial").exists():
                missing = sorted(set(range(1, meta["chunks"] + 1)) - set(_received(d)))
                if missing:
                    raise HTTPException(409, {"error": "не все фрагменты", "missing": missing[:100]})
                h, size, sums = hashlib.sha256(), 0, []
                for n in range(1, meta["chunks"] + 1):
                    ch = hashlib.sha256()
                    with open(_chunk(d, n), "rb") as f:
                        while block := f.read(1 << 20):
                            h.update(block)
                            ch.update(block)
                            size += len(block)
                    if ch.hexdigest() != meta["chunk_sha256"].get(str(n)):
                        raise HTTPException(409, {"error": f"фрагмент {n} повреждён на сервере"})
                    sums.append(ch.hexdigest())
                if size != meta["cipher_bytes"] or h.hexdigest() != meta["cipher_sha256"]:
                    raise HTTPException(409, {"error": "размер или sha256 шифротекста не совпал"})
                meta["completed"] = time.time()
                _write_json(d / "meta.json", meta)
                (d / ".partial").unlink()
            sums = [meta["chunk_sha256"][str(n)] for n in range(1, meta["chunks"] + 1)]
            return {"ok": True, "chunk_sha256": sums}

    @app.get("/v1/packages/{pid}/chunks/{n}")
    async def get_chunk(pid: str, n: int):
        d, meta = _meta(pid)
        if (d / ".partial").exists():
            raise HTTPException(409, "отправка ещё не закончена")
        f = _chunk(d, n)
        if not 1 <= n <= meta["chunks"] or not f.exists():
            raise HTTPException(404, "нет такого фрагмента")
        if n == 1:
            async with _lock(pid):
                meta = _read_json(d / "meta.json")
                meta["downloads"] = meta.get("downloads", 0) + 1
                meta["last_download"] = time.time()
                _write_json(d / "meta.json", meta)
        return FileResponse(f, media_type="application/octet-stream")

    def _delete(pid: str, body: dict, status: str) -> dict:
        d, meta = _meta(pid)
        if not hmac.compare_digest(_hash_token(str(body.get("delete_token") or "")), meta["delete_hash"]):
            raise HTTPException(403, "токен не подходит")
        _remove(pid, d, status)
        return {"ok": True, "status": status}

    @app.post("/v1/packages/{pid}/confirm")
    async def confirm(pid: str, body: dict = Body(...)) -> dict:
        async with _lock(pid):
            return _delete(pid, body, "confirmed")

    @app.post("/v1/packages/{pid}/cancel")
    async def cancel(pid: str, body: dict = Body(...)) -> dict:
        async with _lock(pid):
            return _delete(pid, body, "cancelled")

    # ---------------------------------------------------------------- связка машин

    @app.put("/v1/groups/{gid}/devices/{dev}")
    async def device_beat(gid: str, dev: str, request: Request) -> dict:
        g = _group(gid) / "devices"
        data = await _json_body(request, DEVICE_MAX)
        g.mkdir(parents=True, exist_ok=True)
        f = g / f"{_dev(dev)}.json"
        if not f.exists() and len(list(g.glob("*.json"))) >= DEVICES_MAX:
            raise HTTPException(429, "слишком много машин в связке")
        _write_json(f, {"dev": dev, "seen": time.time(), "ip": request.client.host if request.client else None,
                        "data": data})
        return {"ok": True}

    @app.get("/v1/groups/{gid}/devices")
    def devices(gid: str) -> dict:
        g = _group(gid) / "devices"
        out = []
        for f in sorted(g.glob("*.json")) if g.exists() else []:
            try:
                out.append(_read_json(f))
            except (OSError, ValueError):
                pass
        return {"devices": out, "now": time.time()}

    @app.delete("/v1/groups/{gid}/devices/{dev}")
    def device_leave(gid: str, dev: str) -> dict:
        (_group(gid) / "devices" / f"{_dev(dev)}.json").unlink(missing_ok=True)
        return {"ok": True}

    def _inbox(gid: str, dev: str) -> Path:
        return _group(gid) / "inbox" / _dev(dev)

    @app.post("/v1/groups/{gid}/inbox/{dev}", status_code=201)
    async def send(gid: str, dev: str, request: Request) -> dict:
        box = _inbox(gid, dev)
        data = await _json_body(request, MESSAGE_MAX)
        box.mkdir(parents=True, exist_ok=True)
        if len(list(box.glob("*.json"))) >= INBOX_MAX:
            raise HTTPException(429, "почтовый ящик переполнен")
        mid = f"{time.time_ns():020d}{secrets.token_hex(4)}"
        # адрес отправителя в tailnet — его подделать нельзя: получатель сверяет его со своим списком машин Tailscale
        _write_json(box / f"{mid}.json", {"id": mid, "time": time.time(),
                                          "from_ip": request.client.host if request.client else None, "data": data})
        return {"ok": True, "id": mid}

    @app.get("/v1/groups/{gid}/inbox/{dev}")
    def inbox(gid: str, dev: str) -> dict:
        box = _inbox(gid, dev)
        out = []
        for f in sorted(box.glob("*.json")) if box.exists() else []:
            try:
                out.append(_read_json(f))
            except (OSError, ValueError):
                pass
        return {"messages": out}

    @app.delete("/v1/groups/{gid}/inbox/{dev}/{mid}")
    def ack(gid: str, dev: str, mid: str) -> dict:
        if not re.match(r"^[0-9a-f]{28}$", mid):
            raise HTTPException(400, "неверный id сообщения")
        (_inbox(gid, dev) / f"{mid}.json").unlink(missing_ok=True)
        return {"ok": True}

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
        return JSONResponse(detail, status_code=exc.status_code)

    return app


# ---------------------------------------------------------------- обслуживание (для владельца сервера)


def list_packages() -> list[dict]:
    root = _store() / "packages"
    out = []
    for d in sorted(root.iterdir()) if root.exists() else []:
        if not d.is_dir() or not (d / "meta.json").exists():
            continue
        m = _read_json(d / "meta.json")
        size = sum(f.stat().st_size for f in (d / "chunks").glob("*.bin"))
        st = ("загружается" if (d / ".partial").exists() else
              f"скачан {m.get('downloads', 0)} раз" if m.get("downloads") else "готов")
        out.append({"id": d.name, "source": m.get("source_host"), "profiles": m.get("profiles"),
                    "created": m.get("created"), "bytes": size, "status": st})
    return out


def remove_package(pid: str) -> bool:
    d = _pkg(pid)
    if not d.exists():
        return False
    _remove(pid, d, "removed")
    return True


__all__ = ["create_app", "list_packages", "remove_package"]
