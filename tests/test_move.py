"""Переезд: ключ, шифрование, упаковка и сквозной сценарий с настоящим xl-relay на localhost."""

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from xlauncher import paths, settings
from xlauncher.core import move, profiles, runstate, service

SECRET = bytes(range(20))


# ---------------------------------------------------------------- ключ


def test_key_format_and_typos():
    key = move.format_key(SECRET, "relay.example:47120")
    assert key == "XL1-000G-40R4-0M30-E209-185G-R38E-1W81-24GK@relay.example:47120"
    assert move.parse_key(key) == (SECRET, "relay.example:47120")
    messy = "  xl1-oo0g-40r4-om3o\n-e2O9-185g-r38e-iw8l-24gk @ Relay.Example:47120 \n"
    assert move.parse_key(messy) == (SECRET, "relay.example:47120")
    assert move.key_id(key) == "24GK"
    for bad in ("", "XL1-000G@relay:1", "XL2-000G-40R4-0M30-E209-185G-R38E-1W81-24GK@r:1",
                "XL1-000G-40R4-0M30-E209-185G-R38E-1W81-24GU@relay:1", key.split("@")[0], key + "x/"):
        with pytest.raises(move.MoveError):
            move.parse_key(bad)
    assert move.normalize_relay("http://100.64.0.1:47120/") == "100.64.0.1:47120"


def test_hkdf_vector_is_stable():
    k = move.derive(SECRET)
    assert k.package_id == "6cb5a0b64013c822d8a06acf2c171c73"
    assert k.enc_key.hex().startswith("b4d4ecf8d935a83a") and k.delete_token.startswith("1c90e7e48c034710")
    assert len({k.package_id, k.enc_key.hex(), k.delete_token}) == 3


# ---------------------------------------------------------------- шифрование


def test_chunks_cannot_be_swapped_truncated_or_opened_with_other_key():
    k = move.derive(SECRET)
    c1 = move.encrypt_chunk(k, 1, b"first", False)
    c2 = move.encrypt_chunk(k, 2, b"last", True)
    assert move.decrypt_chunk(k, 1, c1, False) == b"first" and move.decrypt_chunk(k, 2, c2, True) == b"last"
    with pytest.raises(move.MoveError):
        move.decrypt_chunk(k, 2, c1, True)  # переставили
    with pytest.raises(move.MoveError):
        move.decrypt_chunk(k, 1, c1, True)  # обрезали: первый выдают за последний
    with pytest.raises(move.MoveError):
        move.decrypt_chunk(move.derive(b"\1" * 20), 1, c1, False)


def test_plain_source_splits_header_and_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(move, "CHUNK", 10)
    z = tmp_path / "z"
    z.write_bytes(b"abcdefghijklmnopqrstuvwxyz")
    src = move.PlainSource(b"HEADER-", z)
    assert src.chunks == 4
    assert b"".join(src.read_chunk(n) for n in range(1, 5)) == b"HEADER-abcdefghijklmnopqrstuvwxyz"


# ---------------------------------------------------------------- упаковка


def _profile(name, payload=b"cookie"):
    p = profiles.create(name)
    d = p.dir / "data" / "Default"
    for rel, data in {
        "Network/Cookies": payload, "Local Storage/leveldb/000003.log": os.urandom(3000),
        "Local Storage/leveldb/LOCK": b"", "Preferences": b"{}",
        "Cache/Cache_Data/data_0": b"x" * 100, "Code Cache/js/index": b"x",
        "Service Worker/CacheStorage/abc": b"x", "Service Worker/Database/000001.log": b"sw",
        "GPUCache/data_1": b"x", "foo.tmp": b"x",
    }.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(data)
    (p.dir / "data" / "SingletonLock").write_bytes(b"")
    (p.dir / "data" / "optimization_guide_model_store").mkdir()
    (p.dir / "data" / "optimization_guide_model_store" / "m").write_bytes(b"x")
    (p.dir / "data" / "BrowserMetrics-spare.pma").write_bytes(b"x")
    return p


def test_collect_excludes_caches_and_locks():
    p = _profile("Анна")
    _, files = move.collect()
    arcs = {a.split(p.slug + "/", 1)[1] for _, a in files}
    assert "data/Default/Network/Cookies" in arcs and "profile.json" in arcs
    assert "data/Default/Service Worker/Database/000001.log" in arcs
    assert "data/Default/Local Storage/leveldb/000003.log" in arcs
    for bad in ("Cache/", "Code Cache/", "CacheStorage", "GPUCache", ".tmp", "Singleton", "optimization_guide",
                "BrowserMetrics", "leveldb/LOCK"):
        assert not any(bad in a for a in arcs), bad


def test_portable_settings_drop_machine_blocks():
    settings.save({"remote": {"token": "t"}, "move": {"relay": "r:1"}, "monitor_interval_min": 3})
    s = move.portable_settings()
    assert "remote" not in s and "move" not in s and s["monitor_interval_min"] == 3


# ---------------------------------------------------------------- сквозной сценарий


def _switch_root(monkeypatch, root: Path):
    monkeypatch.setenv("XL_ROOT", str(root))
    paths.ensure()
    (root / "profiles").mkdir(exist_ok=True)


def _tree(d: Path) -> dict:
    return {str(f.relative_to(d)): f.read_bytes() for f in sorted(d.rglob("*")) if f.is_file()}


def test_export_freeze_receive_confirm(relay, tmp_path, monkeypatch, xl_root):
    addr, store = relay
    monkeypatch.setattr(move, "CHUNK", 64 * 1024)  # несколько фрагментов на маленьких данных
    a1 = _profile("Анатолий", os.urandom(150_000))
    _profile("Пётр")
    settings.save({"remote": {"enabled": True, "token": "A"}, "monitor_interval_min": 7})
    (xl_root / "warmup").mkdir()
    (xl_root / "warmup" / "sites.json").write_text('{"sites": []}', encoding="utf-8")
    before = _tree(a1.dir)
    seen = []
    st = move.export(addr, progress=lambda stage, done, total, **kw: seen.append(stage))
    assert st["phase"] == "sent" and st["chunks"] > 2 and {"pack", "encrypt", "upload"} <= set(seen)
    key = st["key"]
    assert move.parse_key(key)[1] == addr
    txt = (xl_root / "MOVE.txt").read_text(encoding="utf-8-sig")
    assert key in txt and "Анатолий" in txt
    assert not (xl_root / "run" / "move" / "out").exists()  # временный архив удалён
    r = service.start(a1.id)
    assert r["ok"] is False and "переданы" in r["error"]  # заморозка
    assert move.remote_status() == "waiting"
    with pytest.raises(move.MoveError):
        move.export(addr)  # второй переезд поверх первого нельзя

    # --- машина B
    b = tmp_path / "b-root"
    _switch_root(monkeypatch, b)
    settings.save({"remote": {"enabled": True, "token": "B"}})
    info = move.inspect(key)
    assert info["source_host"] and {p["name"] for p in info["profiles"]} == {"Анатолий", "Пётр"}
    assert info["conflicts"] == []
    res = move.receive(key)
    assert res["confirmed"] and sorted(res["added"]) == ["Анатолий", "Пётр"]
    got = profiles.get(a1.id)
    after = _tree(got.dir)
    assert f"Перенесён с {info['source_host']}" in after.pop("launch.log").decode()
    want = {k: v for k, v in before.items() if k in after}
    assert after == want and "data/Default/Network/Cookies" in after
    assert not any("Cache_Data" in k for k in after)
    s = settings.load()
    assert s["remote"]["token"] == "B" and s["monitor_interval_min"] == 7  # своё remote осталось
    assert (b / "warmup" / "sites.json").exists()
    assert not [d for d in (store / "packages").iterdir() if d.is_dir()]  # пакет удалён с сервера после проверки
    assert not (b / "run" / "move" / "in" / info["package_id"]).exists()

    # --- снова A: видит, что приём прошёл; отменить уже нельзя
    _switch_root(monkeypatch, tmp_path / "xlroot")
    assert move.remote_status() == "confirmed"
    with pytest.raises(move.MoveError, match="уже принят"):
        move.cancel_export()


def test_upload_resume_after_break(relay, monkeypatch, xl_root):
    addr, store = relay
    monkeypatch.setattr(move, "CHUNK", 32 * 1024)
    _profile("Один", os.urandom(200_000))
    stop = threading.Event()

    def progress(stage, done, total, **kw):
        if stage == "upload" and done == 3:
            stop.set()  # «обрыв» после третьего фрагмента

    with pytest.raises(move.Cancelled):
        move.export(addr, progress=progress, cancel=stop)
    st = move.load_state()
    assert st["phase"] == "uploading" and move.frozen_message()
    pkg = store / "packages" / st["package_id"] / "chunks"
    assert len(list(pkg.glob("*.bin"))) == 3
    uploaded = []
    st = move.upload(progress=lambda stage, done, total, **kw: uploaded.append(done))
    assert st["phase"] == "sent" and len(uploaded) == st["chunks"] - 3  # дослал только недостающие


def test_corrupted_chunk_is_rejected_and_package_stays(relay, tmp_path, monkeypatch):
    addr, store = relay
    monkeypatch.setattr(move, "CHUNK", 32 * 1024)
    _profile("Два", os.urandom(100_000))
    key = move.export(addr)["key"]
    pid = move.derive(move.parse_key(key)[0]).package_id
    f = store / "packages" / pid / "chunks" / "000002.bin"
    f.write_bytes(bytes([f.read_bytes()[0] ^ 1]) + f.read_bytes()[1:])
    _switch_root(monkeypatch, tmp_path / "b")
    with pytest.raises(move.MoveError):
        move.receive(key)
    assert (store / "packages" / pid).exists()  # не подтверждено — пакет на месте
    assert profiles.load_all()[0] == []  # ничего не перенесено


def test_cancel_unfreezes(relay, monkeypatch, xl_root):
    addr, store = relay
    p = _profile("Три")
    st = move.export(addr)
    assert move.frozen_message()
    assert move.cancel_export()["status"] == "cancelled"
    assert not move.frozen_message() and not (xl_root / "MOVE.txt").exists()
    assert not [d for d in (store / "packages").iterdir() if d.is_dir()]
    monkeypatch.setattr(service.engine, "find_exe", lambda v: None)
    monkeypatch.setattr(service.engine.installer, "start", lambda v: None)
    assert "переданы" not in (service.start(p.id).get("error") or "")
    with pytest.raises(move.MoveError, match="отменён"):
        move.inspect(st["key"])


def test_running_profile_blocks_export(relay, monkeypatch):
    addr, _ = relay
    p = _profile("Четыре")
    monkeypatch.setattr(runstate, "is_active", lambda pid: pid == p.id)
    with pytest.raises(move.MoveError, match="остановите"):
        move.export(addr)
    assert not move.load_state()


def test_conflicts_and_backup_existing(relay, tmp_path, monkeypatch):
    addr, _ = relay
    src = _profile("Общий", b"new-cookie")
    key = move.export(addr)["key"]
    # B уже имеет этот же профиль (раньше переезжал) и ещё один свой
    b = tmp_path / "b"
    _switch_root(monkeypatch, b)
    import shutil

    shutil.copytree(tmp_path / "xlroot" / "profiles" / src.slug, b / "profiles" / src.slug)
    (b / "profiles" / src.slug / "data" / "Default" / "Network" / "Cookies").write_bytes(b"old-cookie")
    own = profiles.create("Свой на B")
    info = move.inspect(key)
    assert [c["id"] for c in info["conflicts"]] == [src.id]
    # по умолчанию — пропустить: старый не тронут
    res = move.receive(key, backup_existing=False)
    assert res["skipped"] == ["Общий"] and not res["added"]
    assert (b / "profiles" / src.slug / "data/Default/Network/Cookies").read_bytes() == b"old-cookie"

    # заново: режим «Забрать сюда» — всё своё уходит в backups/…-before-move
    _switch_root(monkeypatch, tmp_path / "xlroot")
    move.state_path().unlink()
    key = move.export(addr)["key"]
    _switch_root(monkeypatch, b)
    res = move.receive(key, backup_existing=True)
    assert res["added"] == ["Общий"] and res["backup"]
    bk = Path(res["backup"])
    assert (bk / own.slug / "profile.json").exists() and (bk / src.slug).exists()
    assert [p.data["name"] for p in profiles.load_all()[0]] == ["Общий"]
    assert (b / "profiles" / src.slug / "data/Default/Network/Cookies").read_bytes() == b"new-cookie"


def test_pending_confirm_retried(relay, tmp_path, monkeypatch):
    addr, store = relay
    _profile("Пять")
    key = move.export(addr)["key"]
    _switch_root(monkeypatch, tmp_path / "b")
    real = move.Relay.finish
    monkeypatch.setattr(move.Relay, "finish", lambda self, *a: (_ for _ in ()).throw(move.MoveError("нет связи")))
    res = move.receive(key)
    assert res["confirmed"] is False and res["added"] == ["Пять"] and move.pending_imports()
    with pytest.raises(move.MoveError):
        move.export(addr)  # пока сервер не подтвердил — свой переезд не начать
    monkeypatch.setattr(move.Relay, "finish", real)
    assert move.retry_pending_confirms() == 1 and not move.pending_imports()
    assert not [d for d in (store / "packages").iterdir() if d.is_dir()]
    json.dumps(res)


def test_api_export_key_and_frozen_status(relay, monkeypatch):
    from fastapi.testclient import TestClient

    from xlauncher.server import create_app

    addr, _ = relay
    _profile("Шесть")
    c = TestClient(create_app(None))
    assert c.post("/api/move/export").json()["error"] == "укажите адрес сервера переезда"
    assert c.post("/api/move/relay", json={"relay": "нет такого"}).status_code == 400
    assert c.post("/api/move/relay", json={"relay": addr}).json()["relay"] == addr
    assert c.get("/api/move/estimate").json()["profiles"] == 1
    c.post("/api/move/export")
    for _ in range(200):
        j = c.get("/api/move/status").json()["job"]
        if not j["running"]:
            break
        time.sleep(0.05)
    assert j["stage"] == "done" and "key" not in j["result"] and "secret" not in j["result"]
    assert c.get("/api/status").json()["move"]["export"] == "sent"
    key = c.get("/api/move/key").json()["key"]
    assert key.startswith("XL1-") and c.get("/api/move/remote").json()["status"] == "waiting"
    assert c.post("/api/move/export/cancel").json()["status"] == "cancelled"
    assert c.get("/api/status").json()["move"]["export"] is None
