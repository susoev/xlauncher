"""Пути внутри корневой папки лаунчера.

Корень задаёт загрузчик через переменную XL_ROOT (папка, где лежит xlauncher.exe).
При запуске из исходников — папка .xlroot в корне репозитория.
"""

from __future__ import annotations

import os
from pathlib import Path


def root() -> Path:
    env = os.environ.get("XL_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / ".xlroot"


def app_dir() -> Path:
    return root() / "app"


def run_dir() -> Path:
    return root() / "run"


def logs_dir() -> Path:
    return root() / "logs"


def ensure() -> None:
    for d in (app_dir(), run_dir(), logs_dir()):
        d.mkdir(parents=True, exist_ok=True)


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_trim(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
