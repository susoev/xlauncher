"""Рекомендованные политики Chromium для всех профилей (Windows, реестр текущего пользователя).

HKCU\\Software\\Policies\\Chromium\\Recommended — значения по умолчанию, которые пользователь может
поменять в настройках браузера. Сайтам политики не видны. Права администратора не нужны.

Поисковик по умолчанию нельзя надёжно задать через Preferences профиля: Chromium защищает его
подписью (Secure Preferences) и сбрасывает ручные правки.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("xlauncher.policies")

KEY = r"Software\Policies\Chromium\Recommended"

GOOGLE = {
    "DefaultSearchProviderEnabled": 1,
    "DefaultSearchProviderName": "Google",
    "DefaultSearchProviderKeyword": "google.com",
    "DefaultSearchProviderSearchURL": "https://www.google.com/search?q={searchTerms}",
    "DefaultSearchProviderSuggestURL": "https://www.google.com/complete/search?client=chrome&q={searchTerms}",
    "DefaultSearchProviderIconURL": "https://www.google.com/favicon.ico",
    "DefaultSearchProviderEncodings": None,  # список — отдельный подключ, не задаём
}


def apply() -> bool:
    """Записывает политики, если они отличаются. True — что-то изменилось."""
    if sys.platform != "win32":
        return False
    import winreg

    changed = False
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
        for name, value in GOOGLE.items():
            if value is None:
                continue
            kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
            try:
                cur, cur_kind = winreg.QueryValueEx(k, name)
                if cur == value and cur_kind == kind:
                    continue
            except FileNotFoundError:
                pass
            winreg.SetValueEx(k, name, 0, kind, value)
            changed = True
    if changed:
        log.info("поисковик по умолчанию для профилей: Google (рекомендованная политика)")
    return changed


def remove() -> None:
    """Убрать то, что ставил лаунчер (перед очисткой компьютера после переезда)."""
    if sys.platform != "win32":
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            for name in GOOGLE:
                try:
                    winreg.DeleteValue(k, name)
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        pass
