"""Брандмауэр Windows для удалённого доступа: разбор проверки, скрипты PowerShell, API."""

from pathlib import Path

from fastapi.testclient import TestClient

from xlauncher.core import firewall, remote


def test_evaluate():
    assert firewall.evaluate({"blocks": 0, "rule": 1, "ports": ["47111"]}, 47111)["ok"] is True
    r = firewall.evaluate({"blocks": 2, "rule": 1, "ports": ["47111"]}, 47111)
    assert r["ok"] is False and r["blocks"] == 2 and r["rule"]
    assert firewall.evaluate({"blocks": 0, "rule": 1, "ports": ["5000"]}, 47111)["ok"] is False  # правило на другой порт
    assert firewall.evaluate({"blocks": 0, "rule": 0, "ports": []}, 47111)["ok"] is False


def test_scripts_quote_paths(tmp_path):
    exe = Path("C:/Users/O'Brien/xl/app/2026.1.1.1/python/XLauncher.exe")
    s = firewall.check_script(exe)
    assert "'C:/Users/O''Brien/" in s and firewall.RULE_NAME in s and "ConvertTo-Json" in s
    f = firewall.fix_script(Path("C:/xl/app"), 47111, tmp_path / "r.txt")
    assert "-LocalPort 47111" in f and "'100.64.0.0/10','fd7a:115c:a1e0::/48'" in f
    assert "Remove-NetFirewallRule" in f and "-Action Allow" in f
    assert "0.0.0.0" not in f and "Any -RemoteAddress" not in f  # только tailnet


def test_app_root(monkeypatch):
    monkeypatch.setattr(firewall, "exe_path", lambda: Path("/x/xl/app/2026.1.1.1/python/XLauncher.exe"))
    assert firewall.app_root() == Path("/x/xl/app")


def test_not_windows_is_noop(monkeypatch):
    monkeypatch.setattr(firewall, "supported", lambda: False)
    assert firewall.check(47111) == {"supported": False, "ok": True}
    assert firewall.fix(47111)["ok"] is True


def test_api(monkeypatch):
    from xlauncher.server import create_app

    calls = []
    monkeypatch.setattr(firewall, "supported", lambda: True)
    monkeypatch.setattr(firewall, "_powershell", lambda script, timeout=0: '{"blocks":1,"rule":0,"ports":[]}')
    monkeypatch.setattr(firewall, "fix", lambda port: calls.append(port) or {"ok": True, "removed_blocks": 1,
                                                                           "firewall": {"ok": True}})
    monkeypatch.setattr(remote.server, "apply", lambda: None)
    c = TestClient(create_app(None))
    assert c.get("/api/status").json()["firewall"] is None  # удалённый доступ выключен — брандмауэр не важен
    r = c.post("/api/remote", json={"enabled": True}).json()
    assert r["firewall"]["ok"] is False and r["firewall"]["blocks"] == 1
    assert c.get("/api/status").json()["firewall"]["ok"] is False
    assert c.post("/api/remote/firewall/fix").json()["removed_blocks"] == 1 and calls == [47111]
    assert c.post("/api/remote/firewall/bogus").status_code == 400
