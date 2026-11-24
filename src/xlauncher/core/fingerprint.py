"""Генерация параметров отпечатка в нейтральном формате (ARCHITECTURE.md §3.1, §4.6)."""

from __future__ import annotations

import secrets

# Страна прокси → язык интерфейса. Остальные — en-US.
COUNTRY_LOCALE = {
    "RU": "ru-RU", "BY": "ru-RU", "KZ": "ru-RU", "UA": "uk-UA",
    "US": "en-US", "CA": "en-CA", "GB": "en-GB", "IE": "en-GB", "AU": "en-AU", "NZ": "en-NZ",
    "IN": "en-IN", "SG": "en-SG", "PH": "en-PH",
    "DE": "de-DE", "AT": "de-AT", "CH": "de-CH", "FR": "fr-FR", "BE": "fr-BE", "ES": "es-ES", "MX": "es-MX",
    "AR": "es-AR", "IT": "it-IT", "PT": "pt-PT", "BR": "pt-BR", "NL": "nl-NL", "PL": "pl-PL", "CZ": "cs-CZ",
    "TR": "tr-TR", "GE": "ka-GE", "AM": "hy-AM", "AZ": "az-AZ", "UZ": "uz-UZ", "RS": "sr-RS",
    "TH": "th-TH", "VN": "vi-VN", "ID": "id-ID", "MY": "ms-MY", "JP": "ja-JP", "KR": "ko-KR",
    "CN": "zh-CN", "TW": "zh-TW", "HK": "zh-HK", "AE": "ar-AE", "IL": "he-IL",
}

# Ядра CPU: реалистичное распределение для ноутбуков/ПК на Windows.
CORES = [4, 6, 8, 8, 8, 12, 12, 16, 16, 20]


def locale_for(country: str | None) -> str:
    return COUNTRY_LOCALE.get((country or "").upper(), "en-US")


def accept_language(locale: str) -> str:
    """Список для --accept-lang: ru-RU,ru,en-US,en. Коэффициенты q Chromium добавит сам."""
    lang = locale.split("-")[0]
    parts = [locale, lang]
    if lang != "en":
        parts += ["en-US", "en"]
    return ",".join(dict.fromkeys(parts))


def generate(country: str | None = None, locale: str | None = None) -> dict:
    loc = locale or locale_for(country)
    return {
        "seed": secrets.randbits(32) or 1,
        "platform": "windows",
        "brand": "Chrome",
        "hardware_concurrency": secrets.choice(CORES),
        "locale": loc,
        "accept_language": accept_language(loc),
        "timezone": "auto",
    }


def regenerate_identity(fp: dict) -> dict:
    """Новый seed и ядра при сохранении языка и часового пояса (для клонов)."""
    new = dict(fp)
    new["seed"] = secrets.randbits(32) or 1
    new["hardware_concurrency"] = secrets.choice(CORES)
    return new
