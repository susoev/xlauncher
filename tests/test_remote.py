"""Удалённый доступ: MCP по HTTP с токеном и фильтром адресов."""

import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from xlauncher import paths
from xlauncher.core import remote


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def app_api(xl_root):
    import uvicorn

    from xlauncher.server import create_app

    port = _port()
    srv = uvicorn.Server(uvicorn.Config(create_app(None), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    (paths.run_dir() / "app.json").write_text(json.dumps({"port": port}))
    yield port
    srv.should_exit = True
    remote.server.stop()


def _get(url, token=None):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


async def _tools(url, token):
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30, trust_env=False)
    async with http, streamable_http_client(url, http_client=http) as (r, w, *_), ClientSession(r, w) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        info = await s.call_tool("machine_info", {})
        return names, info.content[0].text


def test_remote_mcp_requires_token(app_api):
    import urllib.request as ur

    port = _port()
    remote.save({"enabled": True, "port": port, "token": "secret-token"})
    remote.server.apply()
    assert remote.server.running, remote.server.error
    url = f"http://127.0.0.1:{port}/mcp"
    assert _get(url) == 401
    assert _get(url, "wrong") == 401
    names, info = asyncio.run(_tools(url, "secret-token"))
    assert {"list_profiles", "start_profile", "browser_snapshot", "warmup_profile", "day_routine", "launch_log", "machine_info"} <= names
    assert "компьютер:" in info and "профилей: 0" in info

    # выключить и включить заново — сервер пересоздаётся
    remote.save({"enabled": False})
    remote.server.apply()
    assert not remote.server.running
    remote.save({"enabled": True})
    remote.server.apply()
    names, _ = asyncio.run(_tools(url, "secret-token"))
    assert "list_profiles" in names


def test_client_address_filter():
    assert remote.client_allowed("127.0.0.1")
    assert remote.client_allowed("100.125.115.71")       # Tailscale
    assert remote.client_allowed("fd7a:115c:a1e0::1")    # Tailscale IPv6
    assert remote.client_allowed("::ffff:100.64.0.1")
    assert not remote.client_allowed("192.168.1.10")     # домашняя сеть
    assert not remote.client_allowed("8.8.8.8")
    assert not remote.client_allowed("")
