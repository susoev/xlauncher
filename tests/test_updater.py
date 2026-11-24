import hashlib
import json
import threading
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from xlauncher.updater import Updater, parse_version


def test_parse_version_order():
    assert parse_version("2026.11.25.3") > parse_version("2026.11.24.15")
    assert parse_version("v2026.1.2.10") > parse_version("2026.1.2.9")
    assert parse_version("0.0.0") < parse_version("2026.1.1.1")
    assert parse_version("мусор") == (0,)


def _zip(path: Path, files: dict[str, str]) -> str:
    with zipfile.ZipFile(path, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def releases(tmp_path):
    """Локальный «GitHub Releases»: /latest/download/manifest.json и /download/v<ver>/<asset>."""
    site = tmp_path / "site"
    (site / "latest" / "download").mkdir(parents=True)
    handler = partial(SimpleHTTPRequestHandler, directory=str(site))
    handler.log_message = lambda *a: None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def publish(version: str, bootstrap_version: int = 0, corrupt: bool = False) -> None:
        d = site / "download" / f"v{version}"
        d.mkdir(parents=True, exist_ok=True)
        asset = f"xlauncher-app-{version}.zip"
        sha = _zip(d / asset, {"src/xlauncher/VERSION": version, "python/pythonw.exe": "stub"})
        m = {"version": version, "app_asset": asset, "app_sha256": "0" * 64 if corrupt else sha}
        if bootstrap_version:
            (d / "xlauncher.exe").write_bytes(b"new-bootstrap")
            m |= {"bootstrap_version": bootstrap_version, "bootstrap_asset": "xlauncher.exe",
                  "bootstrap_sha256": hashlib.sha256(b"new-bootstrap").hexdigest()}
        (site / "latest" / "download" / "manifest.json").write_text(json.dumps(m))

    yield f"http://127.0.0.1:{srv.server_address[1]}", publish
    srv.shutdown()


def make_root(tmp_path: Path, current: str, bootstrap_version: int = 1) -> Path:
    root = tmp_path / "root"
    (root / "app" / current).mkdir(parents=True)
    (root / "run").mkdir()
    (root / "app" / "current.txt").write_text(current)
    (root / "run" / "bootstrap.json").write_text(json.dumps({"version": bootstrap_version}))
    (root / "xlauncher.exe").write_bytes(b"old-bootstrap")
    return root


def test_installs_newer_version_and_switches(tmp_path, releases):
    base, publish = releases
    root = make_root(tmp_path, "2026.1.1.1")
    publish("2026.1.2.1")
    ready = []
    u = Updater("2026.1.1.1", ready.append, releases=base, root=root)
    assert u.check_once() == "2026.1.2.1"
    assert ready == ["2026.1.2.1"]
    assert (root / "app" / "current.txt").read_text() == "2026.1.2.1"
    assert (root / "app" / "previous.txt").read_text() == "2026.1.1.1"
    assert (root / "app" / "2026.1.2.1" / "src" / "xlauncher" / "VERSION").read_text() == "2026.1.2.1"
    assert not list((root / "app").glob("*.part")) and not list((root / "app").glob("*.partial"))


def test_up_to_date_does_nothing(tmp_path, releases):
    base, publish = releases
    root = make_root(tmp_path, "2026.1.2.1")
    publish("2026.1.2.1")
    u = Updater("2026.1.2.1", lambda v: None, releases=base, root=root)
    assert u.check_once() is None
    assert u.state == "up-to-date"


def test_bad_version_skipped(tmp_path, releases):
    base, publish = releases
    root = make_root(tmp_path, "2026.1.1.1")
    (root / "app" / "bad.txt").write_text("2026.1.2.1\n")
    publish("2026.1.2.1")
    u = Updater("2026.1.1.1", lambda v: None, releases=base, root=root)
    assert u.check_once() is None
    assert (root / "app" / "current.txt").read_text() == "2026.1.1.1"


def test_sha_mismatch_rejected(tmp_path, releases):
    base, publish = releases
    root = make_root(tmp_path, "2026.1.1.1")
    publish("2026.1.2.1", corrupt=True)
    u = Updater("2026.1.1.1", lambda v: None, releases=base, root=root)
    assert u.check_once() is None
    assert u.state == "error" and "SHA-256" in u.last_error
    assert (root / "app" / "current.txt").read_text() == "2026.1.1.1"
    assert not (root / "app" / "2026.1.2.1").exists()


def test_bootstrap_replaced_only_when_newer(tmp_path, releases):
    base, publish = releases
    root = make_root(tmp_path, "2026.1.1.1", bootstrap_version=1)
    publish("2026.1.2.1", bootstrap_version=2)
    Updater("2026.1.1.1", lambda v: None, releases=base, root=root).check_once()
    assert (root / "xlauncher.exe").read_bytes() == b"new-bootstrap"
    assert (root / "xlauncher.old.exe").read_bytes() == b"old-bootstrap"
    assert (root / "run" / "bootstrap-replaced").exists()

    root2 = make_root(tmp_path / "b", "2026.1.1.1", bootstrap_version=2)
    publish("2026.1.3.1", bootstrap_version=2)
    Updater("2026.1.1.1", lambda v: None, releases=base, root=root2).check_once()
    assert (root2 / "xlauncher.exe").read_bytes() == b"old-bootstrap"
    assert not (root2 / "run" / "bootstrap-replaced").exists()


def test_cleanup_keeps_current_and_previous(tmp_path):
    root = make_root(tmp_path, "2026.1.3.1")
    for v in ("2026.1.1.1", "2026.1.2.1"):
        (root / "app" / v).mkdir()
    (root / "app" / "previous.txt").write_text("2026.1.2.1")
    Updater("2026.1.3.1", lambda v: None, root=root).cleanup()
    assert sorted(p.name for p in (root / "app").iterdir() if p.is_dir()) == ["2026.1.2.1", "2026.1.3.1"]
