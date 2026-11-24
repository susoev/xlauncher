import os

import pytest


@pytest.fixture(autouse=True)
def xl_root(tmp_path, monkeypatch):
    """Каждый тест — в своей корневой папке; системные прокси не мешают локальным соединениям."""
    root = tmp_path / "xlroot"
    monkeypatch.setenv("XL_ROOT", str(root))
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    from xlauncher import paths

    paths.ensure()
    os.makedirs(root / "profiles", exist_ok=True)
    return root
