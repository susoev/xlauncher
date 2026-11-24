"""Адаптер движка fingerprint-chromium: установка версий и флаги запуска (ARCHITECTURE.md §4)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import urllib.request
import zipfile
from pathlib import Path

from .. import paths

log = logging.getLogger("xlauncher.engine")

NAME = "fingerprint-chromium"
REPO = "adryfish/fingerprint-chromium"
UA = {"User-Agent": "xlauncher"}


def parse_version(v: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in (v or "").split("."))
    except ValueError:
        return (0,)


def engines_dir() -> Path:
    return paths.root() / "engines" / NAME


def find_exe(version: str) -> Path | None:
    override = os.environ.get("XL_ENGINE_EXE")  # для тестов
    if override:
        return Path(override)
    d = engines_dir() / version
    if not d.is_dir():
        return None
    return next(d.rglob("chrome.exe"), None)


def installed_versions() -> list[str]:
    d = engines_dir()
    if not d.is_dir():
        return []
    return sorted((p.name for p in d.iterdir() if p.is_dir() and find_exe(p.name)), key=parse_version)


# ---------------------------------------------------------------- установка


class Installer:
    """Фоновая установка версии движка с прогрессом для интерфейса."""

    def __init__(self) -> None:
        self.state: dict = {"status": "idle"}
        self._lock = threading.Lock()

    def start(self, version: str) -> None:
        with self._lock:
            if self.state.get("status") == "downloading":
                return
            self.state = {"status": "downloading", "version": version, "done_mb": 0, "total_mb": None}
        threading.Thread(target=self._run, args=(version,), name="engine-install", daemon=True).start()

    def _run(self, version: str) -> None:
        try:
            install(version, self._progress)
            self.state = {"status": "installed", "version": version}
        except Exception as e:  # noqa: BLE001
            log.exception("установка движка %s", version)
            self.state = {"status": "error", "version": version, "error": f"{type(e).__name__}: {e}"}

    def _progress(self, done: int, total: int | None) -> None:
        self.state["done_mb"] = round(done / 1e6)
        self.state["total_mb"] = round(total / 1e6) if total else None


installer = Installer()


def _pick_asset(assets: list[dict]) -> dict | None:
    for a in assets:
        n = a["name"].lower()
        if n.endswith(".zip") and "windows" in n and "x64" in n:
            return a
    for a in assets:
        n = a["name"].lower()
        if n.endswith(".zip") and "win" in n:
            return a
    return None


def install(version: str, progress=lambda done, total: None) -> Path:
    if exe := find_exe(version):
        return exe
    rel = json.loads(urllib.request.urlopen(
        urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/tags/{version}", headers=UA),
        timeout=30).read())
    asset = _pick_asset(rel.get("assets", []))
    if not asset:
        raise RuntimeError(f"в релизе {version} нет сборки для Windows")
    base = engines_dir()
    base.mkdir(parents=True, exist_ok=True)
    part = base / f"{version}.zip.part"
    with urllib.request.urlopen(urllib.request.Request(asset["browser_download_url"], headers=UA),
                                timeout=60) as r, part.open("wb") as f:
        total = int(r.headers.get("Content-Length") or asset.get("size") or 0) or None
        done = 0
        for chunk in iter(lambda: r.read(1 << 20), b""):
            f.write(chunk)
            done += len(chunk)
            progress(done, total)
    staging = base / f"{version}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        with zipfile.ZipFile(part) as z:
            z.extractall(staging)
        final = base / version
        shutil.rmtree(final, ignore_errors=True)
        os.replace(staging, final)
    finally:
        part.unlink(missing_ok=True)
    exe = find_exe(version)
    if not exe:
        raise RuntimeError("в архиве движка нет chrome.exe")
    log.info("движок %s установлен", version)
    return exe


# ---------------------------------------------------------------- запуск


def build_args(*, data_dir: Path, cache_dir: Path, fingerprint: dict, proxy_port: int, timezone: str | None,
               cdp_port: int | None = None, urls: list[str] | None = None) -> list[str]:
    fp = fingerprint
    args = [
        f"--user-data-dir={data_dir}",
        f"--disk-cache-dir={cache_dir}",
        f"--fingerprint={fp['seed']}",
        f"--fingerprint-platform={fp.get('platform', 'windows')}",
        f"--fingerprint-brand={fp.get('brand', 'Chrome')}",
        f"--fingerprint-hardware-concurrency={fp['hardware_concurrency']}",
        f"--lang={fp['locale']}",
        f"--accept-lang={fp['accept_language']}",
        f"--proxy-server=http://127.0.0.1:{proxy_port}",
        "--disable-non-proxied-udp",
        "--disable-quic",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if timezone:
        args.append(f"--timezone={timezone}")
    if cdp_port:
        args += [f"--remote-debugging-port={cdp_port}", "--remote-debugging-address=127.0.0.1"]
    return args + list(urls or [])
