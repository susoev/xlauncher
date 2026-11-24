"""Геолокация IP: страна, город, часовой пояс, провайдер.

Сейчас — онлайн-сервисы через тот же прокси. Офлайн-база DB-IP — этап 6 (ARCHITECTURE.md §6).
"""

from __future__ import annotations

import json

from .proxy import http_get

EMPTY = {"country": None, "city": None, "timezone": None, "asn": None, "source": None}


def lookup(ip: str, proxy_port: int | None = None) -> dict:
    try:
        d = json.loads(http_get(f"https://ipwho.is/{ip}", proxy_port))
        if d.get("success"):
            conn = d.get("connection") or {}
            return {
                "country": d.get("country_code"),
                "city": d.get("city"),
                "timezone": (d.get("timezone") or {}).get("id"),
                "asn": f"AS{conn.get('asn')} {conn.get('isp') or ''}".strip(),
                "source": "ipwho.is",
            }
    except Exception:  # noqa: BLE001
        pass
    try:
        d = json.loads(http_get(f"http://ip-api.com/json/{ip}?fields=status,countryCode,city,timezone,as", proxy_port))
        if d.get("status") == "success":
            return {"country": d["countryCode"], "city": d["city"], "timezone": d["timezone"], "asn": d["as"],
                    "source": "ip-api.com"}
    except Exception:  # noqa: BLE001
        pass
    return dict(EMPTY)
