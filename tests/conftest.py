import os
import socket
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def xl_root(tmp_path, monkeypatch):
    """Каждый тест — в своей корневой папке; системные прокси не мешают локальным соединениям."""
    root = tmp_path / "xlroot"
    monkeypatch.setenv("XL_ROOT", str(root))
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XL_TAILSCALE_EXE", str(tmp_path / "нет-tailscale.exe"))  # не трогать настоящую сеть
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    from xlauncher import paths

    paths.ensure()
    os.makedirs(root / "profiles", exist_ok=True)
    return root


@pytest.fixture
def relay(tmp_path, monkeypatch):
    import uvicorn

    from xl_relay import app as relay_app

    monkeypatch.setenv("XL_RELAY_STORE", str(tmp_path / "relay-store"))
    monkeypatch.setenv("XL_RELAY_MIN_FREE_GB", "0")
    from xlauncher.core import move

    monkeypatch.setattr(move, "RETRY_DELAYS", (0.05, 0.05))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(relay_app.create_app(), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    yield f"127.0.0.1:{port}", tmp_path / "relay-store"
    server.should_exit = True
    t.join(5)


