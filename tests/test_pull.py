"""«Забрать сюда»: связка, запрос B → A, отправка на A без человека, приём на B, очистка A после confirm."""

import os
import time
from pathlib import Path

import pytest

from xlauncher import lifecycle, paths, settings
from xlauncher.core import move, profiles, pull


def _root(monkeypatch, root: Path):
    monkeypatch.setenv("XL_ROOT", str(root))
    paths.ensure()
    (root / "profiles").mkdir(exist_ok=True)


def _wait_job():
    for _ in range(400):
        if not move.job.running:
            return move.job.snapshot()
        time.sleep(0.05)
    raise AssertionError("задача не закончилась")


def test_pair_code_roundtrip_and_isolation():
    code = pull.new_pair_code("relay.example:47120")
    assert code.startswith("XLP-") and code.endswith("@relay.example:47120")
    a, b = pull.Pair(code), pull.Pair(code.lower().replace("-", " "))
    assert a.gid == b.gid and a.key == b.key
    env = a.seal("inbox/x", {"type": "pull"})
    assert b.open("inbox/x", env) == {"type": "pull"}
    assert b.open("inbox/y", env) is None  # письмо другой машине не подделать
    assert pull.Pair(pull.new_pair_code("relay.example:47120")).open("inbox/x", env) is None  # чужая связка
    with pytest.raises(move.MoveError):
        pull.parse_pair_code("XL1-0000@relay:1")


def test_seal_only_for_requester():
    priv, pub = pull.keypair()
    sealed = pull.seal_to(pub, b"XL1-secret")
    assert pull.unseal(priv, sealed) == b"XL1-secret"
    with pytest.raises(move.MoveError):
        pull.unseal(pull.keypair()[0], sealed)


def test_pull_end_to_end(relay, tmp_path, monkeypatch, xl_root):
    addr, store = relay
    monkeypatch.setattr(move, "CHUNK", 64 * 1024)
    monkeypatch.setattr(pull.worker, "wake", lambda: None)
    exits = []
    monkeypatch.setattr(lifecycle, "request_exit", lambda code: exits.append(code))
    monkeypatch.setenv("XL_BOOTSTRAP_VERSION", "5")
    a_root, b_root = xl_root, tmp_path / "b"
    code = pull.new_pair_code(addr)
    wa, wb = pull.Worker(), pull.Worker()

    # A — домашний: профили, связка, heartbeat
    pa = profiles.create("Дома-1")
    (pa.dir / "data" / "Default").mkdir(parents=True)
    (pa.dir / "data" / "Default" / "Cookies").write_bytes(os.urandom(200_000))
    profiles.create("Дома-2")
    pull.set_pair(code)
    wa.tick()

    # B — рабочий: свой старый профиль, та же связка
    _root(monkeypatch, b_root)
    own = profiles.create("Рабочий старый")
    pull.set_pair(code)
    devs = pull.devices()["devices"]
    assert len(devs) == 1 and devs[0]["profiles"] == 2 and devs[0]["online"] and devs[0]["can_pull"]
    st = pull.start_pull(devs[0]["id"])
    assert st["phase"] == "requested" and "priv" not in st

    # A получает запрос и отправляет сам
    _root(monkeypatch, a_root)
    wa.tick()
    j = _wait_job()
    assert j["stage"] == "done", j
    sa = move.load_state()
    assert sa["pull"]["to_name"] and not (a_root / "MOVE.txt").exists()  # ключ ушёл B, не в файл

    # B: прогресс, ключ, приём
    _root(monkeypatch, b_root)
    wb.tick()
    assert pull.load_pull()["phase"] == "receiving"
    j = _wait_job()
    assert j["stage"] == "done", j
    wb.tick()
    pst = pull.load_pull()
    assert pst["phase"] == "done" and "key" not in pst
    names = sorted(p.data["name"] for p in profiles.load_all()[0])
    assert names == ["Дома-1", "Дома-2"]
    backup = Path(pst["result"]["backup"])
    assert backup.name.endswith("-before-move") and (backup / own.slug / "profile.json").exists()
    got = profiles.get(pa.id)
    assert (got.dir / "data" / "Default" / "Cookies").read_bytes() == (pa.dir / "data" / "Default" / "Cookies").read_bytes()

    # A видит confirm и очищает себя: запрос загрузчику, код 76, MOVE.txt не оставлять
    _root(monkeypatch, a_root)
    wa._last_moved_check = 0
    wa.tick()
    time.sleep(1.3)
    assert exits == [lifecycle.EXIT_WIPE]
    req = (a_root / "run" / "wipe-request.json").read_text()
    assert '"keep": ["xlauncher.exe"]' in req
    assert settings.load()["move"]["pair"] == code


def test_pull_cancel_unfreezes_a(relay, tmp_path, monkeypatch, xl_root):
    addr, store = relay
    monkeypatch.setattr(pull.worker, "wake", lambda: None)
    code = pull.new_pair_code(addr)
    profiles.create("A-1")
    pull.set_pair(code)
    wa = pull.Worker()
    wa.tick()
    _root(monkeypatch, tmp_path / "b")
    pull.set_pair(code)
    pull.start_pull(pull.devices()["devices"][0]["id"])
    _root(monkeypatch, xl_root)
    wa.tick()
    _wait_job()
    assert move.frozen_message()
    _root(monkeypatch, tmp_path / "b")
    pull.cancel_pull()
    assert pull.load_pull() is None
    _root(monkeypatch, xl_root)
    wa.tick()
    assert not move.frozen_message()  # A разморожен, пакет на релее отменён
    assert not [d for d in (store / "packages").iterdir() if d.is_dir()]


def test_old_request_ignored_and_foreign_messages_dropped(relay, monkeypatch, xl_root):
    addr, _ = relay
    monkeypatch.setattr(pull.worker, "wake", lambda: None)
    code = pull.new_pair_code(addr)
    profiles.create("A-1")
    pull.set_pair(code)
    p = pull.pair()
    me = pull.device_id()
    pull.send(p, me, {"type": "pull", "req": "old", "from": "0" * 32, "pub": "00" * 32, "time": time.time() - 3600})
    other = pull.Pair(pull.new_pair_code(addr))
    cl = move.Relay(addr)
    cl._call("POST", f"/groups/{p.gid}/inbox/{me}", json=other.seal(f"inbox/{me}", {"type": "pull", "req": "x",
                                                                                      "time": time.time()}))
    cl.close()
    pull.Worker().tick()
    assert not move.job.running and not move.load_state()


def _net(os_name="windows", user=7):
    return {"tailnet": "T-net-1", "user": 7, "self_ips": ["100.64.0.2"],
            "peers": [{"ip": "127.0.0.1", "ips": ["127.0.0.1"], "host": "peer", "os": os_name, "online": True,
                       "user": user}]}


def test_relay_found_in_tailnet(relay, monkeypatch):
    addr, _ = relay
    from xlauncher.core import tailscale

    monkeypatch.setattr(tailscale, "network", lambda: _net("linux"))
    monkeypatch.setattr(move, "RELAY_PORT", int(addr.split(":")[1]))
    assert move.relay_setting() == ""
    assert move.relay_auto() == addr and move.relay_setting() == addr  # найден и запомнен
    monkeypatch.setattr(tailscale, "network", lambda: {**_net(), "peers": []})
    with pytest.raises(move.MoveError, match="не найден"):
        settings.save({"move": {}})
        move.relay_auto()


@pytest.mark.parametrize("os_name, user, accepted", [("windows", 7, True), ("linux", 7, False), ("windows", 8, False)])
def test_auto_pair_accepts_only_own_windows_machines(relay, tmp_path, monkeypatch, xl_root, os_name, user, accepted):
    addr, _ = relay
    from xlauncher.core import tailscale

    monkeypatch.setattr(pull.worker, "wake", lambda: None)
    monkeypatch.setattr(pull, "_net_cache", {})
    monkeypatch.setattr(tailscale, "network", lambda: _net(os_name, user))
    monkeypatch.setattr(move, "RELAY_PORT", int(addr.split(":")[1]))
    profiles.create("Дом")
    wa = pull.Worker()
    wa.tick()  # A: релей найден сам, heartbeat без всякого кода
    assert pull.pair().auto and settings.load()["move"]["relay"] == addr
    _root(monkeypatch, tmp_path / "b")
    devs = pull.devices()["devices"]
    if not accepted:
        assert devs == []  # чужую/не-Windows машину даже не показываем
        return
    assert [d["profiles"] for d in devs] == [1]
    pull.start_pull(devs[0]["id"])
    _root(monkeypatch, xl_root)
    wa.tick()
    assert _wait_job()["stage"] == "done"
    assert move.load_state()["pull"]


def test_auto_pair_rejects_request_from_foreign_ip(relay, tmp_path, monkeypatch, xl_root):
    addr, _ = relay
    from xlauncher.core import tailscale

    monkeypatch.setattr(pull.worker, "wake", lambda: None)
    monkeypatch.setattr(pull, "_net_cache", {})
    monkeypatch.setattr(move, "RELAY_PORT", int(addr.split(":")[1]))
    monkeypatch.setattr(tailscale, "network", lambda: _net("windows", 7))
    profiles.create("Дом")
    wa = pull.Worker()
    wa.tick()
    me = pull.device_id()
    # запрос пришёл через релей с адреса, которого нет среди Windows-машин владельца
    monkeypatch.setattr(pull, "_net_cache", {})
    monkeypatch.setattr(tailscale, "network", lambda: _net("linux", 7))
    p = pull.pair()
    priv, pub = pull.keypair()
    pull.send(p, me, {"type": "pull", "req": "evil", "from": "0" * 32, "from_name": "vps", "pub": pub,
                      "time": time.time()})
    wa.tick()
    assert not move.job.running and not move.load_state()
