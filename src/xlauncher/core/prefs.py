"""Настройки браузера профиля, которые лаунчер выставляет перед каждым запуском.

Пишутся в data/Default/Preferences, пока браузер закрыт. Chromium дополняет файл сам.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

BLOCK = 2

# Что лаунчер выставляет принудительно.
ENFORCED = {
    # Геолокация: сайты сразу получают отказ. Иначе браузер может взять координаты у Windows
    # (по Wi-Fi) — и сайт увидит реальное местоположение рядом с IP прокси.
    ("profile", "default_content_setting_values", "geolocation"): BLOCK,
}


def apply(data_dir: Path) -> None:
    p = data_dir / "Default" / "Preferences"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        prefs = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(prefs, dict):
            prefs = {}
    except (OSError, ValueError):
        prefs = {}
    changed = False
    for path, value in ENFORCED.items():
        node = prefs
        for k in path[:-1]:
            if not isinstance(node.get(k), dict):
                node[k] = {}
            node = node[k]
        if node.get(path[-1]) != value:
            node[path[-1]] = value
            changed = True
    # Разрешения на геолокацию, выданные сайтам раньше, тоже убираем.
    exc = prefs.get("profile", {}).get("content_settings", {}).get("exceptions", {})
    if isinstance(exc, dict) and exc.get("geolocation"):
        exc["geolocation"] = {}
        changed = True
    if changed:
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(prefs), encoding="utf-8")
        os.replace(tmp, p)
