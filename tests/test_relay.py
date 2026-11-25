"""Сервер переезда xl-relay: пакеты (загрузка, докачка, проверка, удаление по токену) и связка машин."""

import hashlib
import os
import secrets

import pytest
from fastapi.testclient import TestClient

from xl_relay import app as relay


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XL_RELAY_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("XL_RELAY_MIN_FREE_GB", "0")
    return TestClient(relay.create_app())


def _package(n=3, size=1000):
    chunks = [os.urandom(size + i) for i in range(n)]
    token = secrets.token_bytes(32)
    body = {"chunks": n, "cipher_bytes": sum(map(len, chunks)),
            "cipher_sha256": hashlib.sha256(b"".join(chunks)).hexdigest(),
            "delete_hash": hashlib.sha256(token).hexdigest(), "source_host": "pc-a", "profiles": 2,
            "created": "2026-11-25T10:00:00+07:00"}
    return secrets.token_hex(16), chunks, token.hex(), body


def _put(c, pid, n, data, sha=None):
    return c.put(f"/v1/packages/{pid}/chunks/{n}", content=data,
                 headers={"X-Chunk-Sha256": sha or hashlib.sha256(data).hexdigest()})


def test_health(client):
    r = client.get("/v1/health").json()
    assert r["ok"] and r["version"] == 1 and r["free_bytes"] > 0


def test_full_cycle_and_confirm(client, tmp_path):
    pid, chunks, token, body = _package()
    assert client.post(f"/v1/packages/{pid}", json=body).status_code == 201
    assert client.post(f"/v1/packages/{pid}", json=body).status_code == 409
    for i, ch in enumerate(chunks, 1):
        assert _put(client, pid, i, ch).status_code == 200
    r = client.post(f"/v1/packages/{pid}/complete")
    assert r.status_code == 200
    assert r.json()["chunk_sha256"] == [hashlib.sha256(c).hexdigest() for c in chunks]
    st = client.get(f"/v1/packages/{pid}").json()
    assert st["complete"] and st["received"] == [1, 2, 3] and "delete_hash" not in st
    got = b"".join(client.get(f"/v1/packages/{pid}/chunks/{i}").content for i in (1, 2, 3))
    assert got == b"".join(chunks)
    assert client.get(f"/v1/packages/{pid}").json()["downloads"] == 1
    assert _put(client, pid, 1, chunks[0]).status_code == 409  # завершённый пакет не переписать
    assert client.post(f"/v1/packages/{pid}/confirm", json={"delete_token": secrets.token_hex(32)}).status_code == 403
    assert client.post(f"/v1/packages/{pid}/confirm", json={"delete_token": token}).status_code == 200
    assert not (tmp_path / "store" / "packages" / pid).exists()
    r = client.get(f"/v1/packages/{pid}")
    assert r.status_code == 410 and r.json()["status"] == "confirmed"  # источник видит, что приём прошёл
    assert client.post(f"/v1/packages/{pid}", json=body).status_code == 409  # id не переиспользуется


def test_resume_and_partial(client):
    pid, chunks, token, body = _package()
    client.post(f"/v1/packages/{pid}", json=body)
    _put(client, pid, 1, chunks[0])
    _put(client, pid, 3, chunks[2])
    st = client.get(f"/v1/packages/{pid}").json()
    assert st["received"] == [1, 3] and not st["complete"]
    r = client.post(f"/v1/packages/{pid}/complete")
    assert r.status_code == 409 and r.json()["missing"] == [2]
    assert client.get(f"/v1/packages/{pid}/chunks/1").status_code == 409  # пока не завершён — не отдаём
    _put(client, pid, 2, chunks[1])
    assert client.post(f"/v1/packages/{pid}/complete").status_code == 200
    assert client.post(f"/v1/packages/{pid}/cancel", json={"delete_token": token}).json()["status"] == "cancelled"


def test_rejects_bad_input(client, tmp_path):
    pid, chunks, token, body = _package()
    assert client.get("/v1/packages/../../etc").status_code in (400, 404)
    assert client.post("/v1/packages/NOT-HEX", json=body).status_code == 400
    assert client.post(f"/v1/packages/{pid}", json={**body, "chunks": 0}).status_code == 400
    client.post(f"/v1/packages/{pid}", json=body)
    assert _put(client, pid, 1, chunks[0], sha="0" * 64).status_code == 400
    assert _put(client, pid, 9, chunks[0]).status_code == 400
    assert client.get(f"/v1/packages/{pid}").json()["received"] == []
    big = os.urandom(relay.CHUNK_MAX + 1)
    assert _put(client, pid, 1, big).status_code == 413
    assert not list((tmp_path / "store" / "packages" / pid / "chunks").glob("*.tmp"))


def test_corrupted_chunk_blocks_complete(client, tmp_path):
    pid, chunks, token, body = _package()
    client.post(f"/v1/packages/{pid}", json=body)
    for i, ch in enumerate(chunks, 1):
        _put(client, pid, i, ch)
    f = tmp_path / "store" / "packages" / pid / "chunks" / "000002.bin"
    f.write_bytes(b"x" + f.read_bytes()[1:])
    r = client.post(f"/v1/packages/{pid}/complete")
    assert r.status_code == 409 and "повреждён" in r.json()["error"]


def test_no_space(client, monkeypatch):
    monkeypatch.setenv("XL_RELAY_MIN_FREE_GB", "100000")
    pid, chunks, token, body = _package()
    assert client.post(f"/v1/packages/{pid}", json=body).status_code == 507


def test_group_devices_and_inbox(client):
    gid, a, b = (secrets.token_hex(16) for _ in range(3))
    assert client.put(f"/v1/groups/{gid}/devices/{a}", json={"name": "A", "sig": "…"}).status_code == 200
    client.put(f"/v1/groups/{gid}/devices/{b}", json={"name": "B"})
    devs = client.get(f"/v1/groups/{gid}/devices").json()["devices"]
    assert sorted(d["data"]["name"] for d in devs) == ["A", "B"]
    assert client.get(f"/v1/groups/{secrets.token_hex(16)}/devices").json()["devices"] == []  # чужая связка пуста
    mid = client.post(f"/v1/groups/{gid}/inbox/{a}", json={"type": "pull", "from": b}).json()["id"]
    msgs = client.get(f"/v1/groups/{gid}/inbox/{a}").json()["messages"]
    assert [m["data"]["type"] for m in msgs] == ["pull"] and msgs[0]["id"] == mid
    assert client.get(f"/v1/groups/{gid}/inbox/{b}").json()["messages"] == []
    client.delete(f"/v1/groups/{gid}/inbox/{a}/{mid}")
    assert client.get(f"/v1/groups/{gid}/inbox/{a}").json()["messages"] == []
    assert client.post(f"/v1/groups/{gid}/inbox/{a}", content=b"x" * (relay.MESSAGE_MAX + 1)).status_code == 413
    client.delete(f"/v1/groups/{gid}/devices/{a}")
    assert [d["data"]["name"] for d in client.get(f"/v1/groups/{gid}/devices").json()["devices"]] == ["B"]


def test_cli_list_and_rm(client, tmp_path):
    pid, chunks, token, body = _package(n=1)
    client.post(f"/v1/packages/{pid}", json=body)
    rows = relay.list_packages()
    assert rows[0]["id"] == pid and rows[0]["status"] == "загружается" and rows[0]["source"] == "pc-a"
    assert relay.remove_package(pid)
    assert relay.list_packages() == []
    assert client.get(f"/v1/packages/{pid}").json()["status"] == "removed"
