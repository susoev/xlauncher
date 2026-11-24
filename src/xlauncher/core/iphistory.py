"""История IP профиля: ip_history.log, строка добавляется только при смене IP (ARCHITECTURE.md §3.2)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

FILE = "ip_history.log"
FIELDS = ("time", "ip", "country", "city", "asn", "source", "flag")


def _clean(v) -> str:
    return str(v or "").replace("\t", " ").replace("\n", " ").strip()


def read(profile_dir: Path, limit: int | None = None) -> list[dict]:
    try:
        lines = (profile_dir / FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for ln in lines:
        if not ln.strip():
            continue
        parts = ln.split("\t") + [""] * len(FIELDS)
        rows.append(dict(zip(FIELDS, parts[: len(FIELDS)])))
    return rows[-limit:] if limit else rows


def last(profile_dir: Path) -> dict | None:
    rows = read(profile_dir, 1)
    return rows[0] if rows else None


def record(profile_dir: Path, ip: str, geo: dict, source: str) -> dict:
    """Добавляет строку, если IP изменился. Возвращает {changed, country_changed, previous}."""
    prev = last(profile_dir)
    if prev and prev["ip"] == ip:
        return {"changed": False, "country_changed": False, "previous": prev}
    country_changed = bool(prev and prev["country"] and geo.get("country") and prev["country"] != geo["country"])
    row = [datetime.now().strftime("%Y-%m-%d %H:%M"), ip, geo.get("country"), geo.get("city"), geo.get("asn"),
           source, "COUNTRY_CHANGED" if country_changed else ""]
    with (profile_dir / FILE).open("a", encoding="utf-8") as f:
        f.write("\t".join(_clean(x) for x in row).rstrip("\t") + "\n")
    return {"changed": True, "country_changed": country_changed, "previous": prev}
