"""Автообновление приложения и загрузчика из GitHub Releases.

Порядок (ARCHITECTURE.md §9):
  1. раз в N минут читаем releases/latest/download/manifest.json;
  2. версия новее текущей и не в app/bad.txt → качаем zip, сверяем SHA-256,
     распаковываем в app/<версия>;
  3. если в манифесте загрузчик новее — подменяем xlauncher.exe (запущенный exe
     в Windows можно переименовать, но не перезаписать);
  4. previous.txt ← текущая, current.txt ← новая, и просим приложение
     перезапуститься (код выхода 75). Проверку новой версии и откат делает загрузчик.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import paths

log = logging.getLogger("xlauncher.updater")

REPO = "susoev/xlauncher"
RELEASES = f"https://github.com/{REPO}/releases"
UA = {"User-Agent": "xlauncher-updater"}


def parse_version(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.strip().lstrip("v").split("."))
    except ValueError:
        return (0,)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract(zip_path: Path, dest: Path) -> None:
    base = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            target = (base / name).resolve()
            if target != base and base not in target.parents:
                raise ValueError(f"недопустимый путь в архиве: {name}")
        z.extractall(base)


class Updater:
    def __init__(
        self,
        current_version: str,
        on_ready: Callable[[str], None],
        interval_s: int = 15 * 60,
        releases: str = RELEASES,
        root: Path | None = None,
    ):
        self.current = current_version
        self.on_ready = on_ready
        self.interval_s = interval_s
        self.releases = releases.rstrip("/")
        self.root = root or paths.root()
        self.app = self.root / "app"
        self.state = "idle"
        self.last_check: str | None = None
        self.last_error: str | None = None
        self.latest_seen: str | None = None
        self.installed: str | None = None
        self._wake = threading.Event()
        self._lock = threading.Lock()

    # ---------------------------------------------------------- сеть

    def _get(self, url: str, timeout: int = 30) -> bytes:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return r.read()

    def _download(self, url: str, dst: Path, sha: str) -> None:
        h = hashlib.sha256()
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=600) as r, dst.open("wb") as f:
            for chunk in iter(lambda: r.read(1 << 20), b""):
                f.write(chunk)
                h.update(chunk)
        if h.hexdigest().lower() != sha.lower():
            dst.unlink(missing_ok=True)
            raise ValueError(f"SHA-256 не совпадает для {url}")

    def fetch_manifest(self) -> dict:
        m = json.loads(self._get(f"{self.releases}/latest/download/manifest.json"))
        for k in ("version", "app_asset", "app_sha256"):
            if not m.get(k):
                raise ValueError(f"в манифесте нет поля {k}")
        return m

    # ---------------------------------------------------------- состояние

    def bad_versions(self) -> set[str]:
        p = self.app / "bad.txt"
        return {x.strip() for x in paths.read_trim(p).splitlines() if x.strip()}

    def bootstrap_version(self) -> int:
        try:
            return int(json.loads((self.root / "run" / "bootstrap.json").read_text())["version"])
        except Exception:  # noqa: BLE001
            return int(os.environ.get("XL_BOOTSTRAP_VERSION") or 0)

    def status(self) -> dict:
        return {
            "current": self.current,
            "state": self.state,
            "last_check": self.last_check,
            "last_error": self.last_error,
            "latest_seen": self.latest_seen,
            "installed": self.installed,
            "bootstrap_version": self.bootstrap_version(),
            "bad_versions": sorted(self.bad_versions()),
        }

    # ---------------------------------------------------------- шаги

    def check_once(self) -> str | None:
        """Проверяет и устанавливает обновление. Возвращает установленную версию или None."""
        with self._lock:
            self.state = "checking"
            self.last_check = datetime.now().isoformat(timespec="seconds")
            try:
                m = self.fetch_manifest()
                v = m["version"]
                self.latest_seen = v
                if parse_version(v) <= parse_version(self.current):
                    self.state = "up-to-date"
                    self.last_error = None
                    return None
                if v in self.bad_versions():
                    self.state = f"версия {v} помечена сбойной, пропускаю"
                    return None
                self.state = f"скачиваю {v}"
                log.info("найдено обновление %s → %s", self.current, v)
                self.install_app(m)
                self.update_bootstrap(m)
                self.switch(v)
                self.installed = v
                self.state = f"установлено {v}, перезапуск"
                self.last_error = None
            except Exception as e:  # noqa: BLE001
                self.state = "error"
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("проверка обновлений: %s", self.last_error)
                return None
        self.on_ready(v)
        return v

    def install_app(self, m: dict) -> None:
        v = m["version"]
        self.app.mkdir(parents=True, exist_ok=True)
        part = self.app / f"{v}.zip.part"
        self._download(f"{self.releases}/download/v{v}/{m['app_asset']}", part, m["app_sha256"])
        try:
            staging = self.app / f"{v}.partial"
            shutil.rmtree(staging, ignore_errors=True)
            _extract(part, staging)
            final = self.app / v
            shutil.rmtree(final, ignore_errors=True)
            os.replace(staging, final)
        finally:
            part.unlink(missing_ok=True)

    def update_bootstrap(self, m: dict) -> None:
        want = int(m.get("bootstrap_version") or 0)
        if not want or want <= self.bootstrap_version() or not m.get("bootstrap_sha256"):
            return
        exe = self.root / "xlauncher.exe"
        new = self.root / "xlauncher.new.exe"
        old = self.root / "xlauncher.old.exe"
        self._download(f"{self.releases}/download/v{m['version']}/{m.get('bootstrap_asset', 'xlauncher.exe')}",
                       new, m["bootstrap_sha256"])
        old.unlink(missing_ok=True)
        if exe.exists():
            os.replace(exe, old)  # запущенный exe переименовать можно
        os.replace(new, exe)
        (self.root / "run" / "bootstrap-replaced").write_text(str(want), encoding="utf-8")
        log.info("загрузчик обновлён до v%s", want)

    def switch(self, v: str) -> None:
        paths.write_atomic(self.app / "previous.txt", self.current)
        paths.write_atomic(self.app / "current.txt", v)

    def cleanup(self) -> None:
        """Оставляет только текущую и предыдущую версии."""
        keep = {paths.read_trim(self.app / "current.txt"), paths.read_trim(self.app / "previous.txt"), self.current}
        try:
            from .core.runstate import active_app_versions

            keep |= active_app_versions()  # из этих версий работают раннеры открытых браузеров
        except Exception:  # noqa: BLE001
            pass
        if not self.app.exists():
            return
        for d in self.app.iterdir():
            if d.is_dir() and d.name not in keep and d.name[:1].isdigit():
                shutil.rmtree(d, ignore_errors=True)

    # ---------------------------------------------------------- фон

    def check_now(self) -> None:
        self._wake.set()

    def run_forever(self, first_delay_s: int = 15) -> None:
        self._wake.wait(first_delay_s)
        while True:
            self._wake.clear()
            if self.check_once():
                return
            self._wake.wait(self.interval_s)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run_forever, name="updater", daemon=True)
        t.start()
        return t

