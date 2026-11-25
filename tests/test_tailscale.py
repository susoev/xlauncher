"""Состояние Tailscale по `tailscale status --json` — с подставным tailscale.exe."""

import json
import sys

import pytest

from xlauncher.core import tailscale

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="подставной exe — shell-скрипт")


def _fake(tmp_path, monkeypatch, payload):
    out = tmp_path / "status.json"
    out.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    exe = tmp_path / "tailscale"
    exe.write_text(f'#!/bin/sh\ncat "{out}"\n', encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("XL_TAILSCALE_EXE", str(exe))


def test_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("XL_TAILSCALE_EXE", str(tmp_path / "нет-такого.exe"))
    assert tailscale.status() == {"state": "missing"}


@pytest.mark.parametrize("payload, state", [
    ({"BackendState": "Running", "Self": {"HostName": "pc", "Online": True, "TailscaleIPs": ["100.64.0.7", "fd7a::7"]}},
     "running"),
    ({"BackendState": "Running", "Self": {"HostName": "pc", "Online": False}}, "offline"),
    ({"BackendState": "NeedsLogin", "AuthURL": "https://login.example/a"}, "needs_login"),
    ({"BackendState": "NeedsMachineAuth"}, "needs_login"),
    ({"BackendState": "Stopped"}, "stopped"),
    ({"BackendState": "Starting"}, "offline"),
    ("failed to connect to local tailscaled", "stopped"),
])
def test_states(tmp_path, monkeypatch, payload, state):
    _fake(tmp_path, monkeypatch, payload)
    st = tailscale.status()
    assert st["state"] == state, st
    if state == "running":
        assert st["host"] == "pc" and st["ip"] == "100.64.0.7"
    if payload == {"BackendState": "NeedsLogin", "AuthURL": "https://login.example/a"}:
        assert st["auth_url"] == "https://login.example/a"


def test_api_status_has_tailscale(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from xlauncher.server import create_app

    _fake(tmp_path, monkeypatch, {"BackendState": "Running", "Self": {"HostName": "pc", "Online": True}})
    c = TestClient(create_app(None))
    assert c.get("/api/tailscale").json()["state"] == "running"
    assert c.get("/api/status").json()["tailscale"]["state"] == "running"  # из кеша, без ожидания
    assert c.post("/api/tailscale/recheck").json()["tailscale"]["state"] == "running"
    assert c.post("/api/tailscale/bogus").json()["ok"] is False
