"""Раннер с заглушкой вместо браузера: проверка прокси, смена страны, запуск и остановка."""

import asyncio
import json
import sys
import time

import pytest

from xlauncher import runner
from xlauncher.core import iphistory, profiles, proxy, runstate

FAKE_CHROME = """#!{python}
import json, sys, time, pathlib
pathlib.Path({out!r}).write_text(json.dumps(sys.argv[1:]))
while True:
    time.sleep(0.2)
"""


@pytest.fixture()
def fake_chrome(tmp_path, monkeypatch):
    out = tmp_path / "chrome-args.json"
    exe = tmp_path / "chrome"
    exe.write_text(FAKE_CHROME.format(python=sys.executable, out=str(out)))
    exe.chmod(0o755)
    monkeypatch.setenv("XL_ENGINE_EXE", str(exe))
    return out


@pytest.fixture()
def fake_net(monkeypatch):
    """Внешний IP и гео без интернета. net['ip'], net['country'] можно менять в тесте."""
    net = {"ip": "203.0.113.5", "country": "RU", "fail": None}

    async def external_ip(fwd):
        if net["fail"]:
            raise proxy.ProxyError(net["fail"])
        return net["ip"]

    def lookup(ip, port=None):
        return {"country": net["country"], "city": "City", "timezone": "Europe/Moscow", "asn": "AS1 Test",
                "source": "test"}

    monkeypatch.setattr(runner, "external_ip", external_ip)
    monkeypatch.setattr("xlauncher.core.geo.lookup", lookup)
    return net


def _run_in_thread(pid, confirm=False):
    import threading

    box = {}

    def target():
        try:
            box["code"] = asyncio.run(runner.run(pid, confirm))
        except runner.Stop as s:
            box["stop"] = s

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, box


def _wait(pred, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_launch_and_stop(fake_chrome, fake_net):
    p = profiles.create("Run", proxy="127.0.0.1:1:u:p", start_urls=["https://example.com"], country="RU")
    t, box = _run_in_thread(p.id)
    assert _wait(lambda: (runstate.read_raw(p.id) or {}).get("status") == "running"), runstate.read_raw(p.id)
    assert _wait(fake_chrome.exists)
    args = json.loads(fake_chrome.read_text())
    st = runstate.read_raw(p.id)
    assert f"--proxy-server=http://127.0.0.1:{st['forwarder_port']}" in args
    assert f"--fingerprint={p.data['fingerprint']['seed']}" in args
    assert "--timezone=Europe/Moscow" in args and "--lang=ru-RU" in args
    assert args[-1] == "https://example.com"
    assert st["ip"] == "203.0.113.5"

    runstate.request_stop(p.id)
    t.join(25)
    assert box.get("code") == runner.EXIT_OK
    assert [r["ip"] for r in iphistory.read(p.dir)] == ["203.0.113.5"]
    log = (p.dir / "launch.log").read_text()
    assert "запуск" in log and "остановлен из лаунчера" in log
    assert profiles.get(p.id).data["machine"]["last_host"]


def test_proxy_failure_stops_before_browser(fake_chrome, fake_net):
    fake_net["fail"] = "127.0.0.1:1: соединение отклонено"
    p = profiles.create("Fail", proxy="127.0.0.1:1:u:p")
    t, box = _run_in_thread(p.id)
    t.join(10)
    s = box["stop"]
    assert s.code == runner.EXIT_PROXY and "прокси не работает" in s.error
    assert not fake_chrome.exists()


def test_country_change_needs_confirmation(fake_chrome, fake_net):
    p = profiles.create("Geo", proxy="127.0.0.1:1:u:p")
    iphistory.record(p.dir, "198.51.100.1", {"country": "TH"}, "launch")
    t, box = _run_in_thread(p.id)
    t.join(10)
    s = box["stop"]
    assert s.code == runner.EXIT_CONFIRM and s.extra["needs_confirmation"]["to"] == "RU"
    assert not fake_chrome.exists()

    t, box = _run_in_thread(p.id, confirm=True)
    assert _wait(fake_chrome.exists)
    runstate.request_stop(p.id)
    t.join(25)
    rows = iphistory.read(p.dir)
    assert rows[-1]["flag"] == "COUNTRY_CHANGED"


def test_no_proxy_refused(fake_chrome, fake_net):
    p = profiles.create("NoProxy")
    t, box = _run_in_thread(p.id)
    t.join(10)
    assert box["stop"].code == runner.EXIT_PROXY and "не задан" in box["stop"].error


def test_engine_downgrade_refused(fake_chrome, fake_net):
    p = profiles.create("New", proxy="127.0.0.1:1:u:p")
    p.data["engine"] = {"last_version": "999.0.0.0"}
    p.save()
    t, box = _run_in_thread(p.id)
    t.join(10)
    assert "более старой версии" in box["stop"].error


def test_dead_runner_reported_as_crashed(xl_root):
    runstate.write("X", status="running", runner_pid=999999)
    assert runstate.read("X")["status"] == "crashed"
    assert not runstate.is_active("X")


def test_geolocation_blocked_before_launch(fake_chrome, fake_net):
    p = profiles.create("GeoBlock", proxy="127.0.0.1:1:u:p")
    pref = p.dir / "data" / "Default" / "Preferences"
    pref.parent.mkdir(parents=True)
    pref.write_text(json.dumps({"profile": {"content_settings": {"exceptions": {
        "geolocation": {"https://x.com,*": {"setting": 1}}}}}, "keep": 1}))
    t, box = _run_in_thread(p.id)
    assert _wait(fake_chrome.exists)
    data = json.loads(pref.read_text())
    assert data["profile"]["default_content_setting_values"]["geolocation"] == 2
    assert data["profile"]["content_settings"]["exceptions"]["geolocation"] == {}
    assert data["keep"] == 1
    runstate.request_stop(p.id)
    t.join(25)
    assert "закрылся за" in (p.dir / "launch.log").read_text()
