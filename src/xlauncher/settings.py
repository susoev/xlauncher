"""Общие настройки лаунчера: <корень>/settings.json (ARCHITECTURE.md §3.3)."""

from __future__ import annotations

import json

from . import paths

DEFAULTS = {
    "engine": "fingerprint-chromium",
    # Закреплённая версия движка: 150 роняет вкладки на canvas (ARCHITECTURE.md §4.3).
    "engine_version": "148.0.7778.215",
    "monitor_interval_min": 10,
    "api_port": 47110,
}


def load() -> dict:
    p = paths.root() / "settings.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **data}


def save(values: dict) -> None:
    current = load()
    current.update(values)
    paths.write_atomic(paths.root() / "settings.json", json.dumps(current, ensure_ascii=False, indent=2))
