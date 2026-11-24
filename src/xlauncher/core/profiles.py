"""Профили: <корень>/profiles/<slug>/profile.json (ARCHITECTURE.md §3, §7).

Имя папки — только подпись; профиль определяется по id внутри profile.json.
"""

from __future__ import annotations

import json
import platform
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import paths
from . import fingerprint

SCHEMA = 1
EDITABLE = {"name", "group", "notes", "tags", "proxy", "start_urls", "agent_access"}
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Что удаляется из копии при «новой личности»: всё, что связывает с исходным профилем.
IDENTITY_FILES = [
    "os_crypt.key", "ip_history.log", "launch.log",
    "data/Local State",
    "data/Default/Network/Cookies", "data/Default/Network/Cookies-journal",
    "data/Default/History", "data/Default/History-journal",
    "data/Default/Login Data", "data/Default/Login Data-journal",
    "data/Default/Web Data", "data/Default/Web Data-journal",
    "data/Default/Local Storage", "data/Default/Session Storage", "data/Default/IndexedDB",
    "data/Default/Service Worker", "data/Default/Sessions", "data/Default/Network/TransportSecurity",
]


def new_id() -> str:
    """ULID: 48 бит времени + 80 бит случайности, Crockford base32."""
    n = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in reversed(range(26)))


def profiles_dir() -> Path:
    return paths.root() / "profiles"


def backups_dir() -> Path:
    return paths.root() / "backups"


def cache_dir(pid: str) -> Path:
    return paths.root() / "cache" / pid


def slugify(name: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", name).strip().strip(".")
    s = re.sub(r"\s+", "-", s)[:60]
    return s or "profile"


@dataclass
class Profile:
    dir: Path
    data: dict

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def slug(self) -> str:
        return self.dir.name

    def save(self) -> None:
        paths.write_atomic(self.dir / "profile.json", json.dumps(self.data, ensure_ascii=False, indent=2))

    def public(self) -> dict:
        d = dict(self.data)
        d["slug"] = self.slug
        return d


def _load(d: Path) -> Profile | None:
    try:
        data = json.loads((d / "profile.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("id"):
        return None
    return Profile(d, migrate(data))


def migrate(data: dict) -> dict:
    """Миграции схемы profile.json. Пока одна версия."""
    data.setdefault("schema", SCHEMA)
    data.setdefault("tags", [])
    data.setdefault("notes", "")
    data.setdefault("start_urls", [])
    data.setdefault("agent_access", False)
    data.setdefault("engine", {})
    data.setdefault("machine", {})
    return data


def load_all() -> tuple[list[Profile], dict[str, list[Profile]]]:
    """Все профили и дубли: {id: [профили с этим id]} для id, встречающихся больше одного раза."""
    root = profiles_dir()
    if not root.is_dir():
        return [], {}
    items = [p for d in sorted(root.iterdir()) if d.is_dir() and (p := _load(d))]
    by_id: dict[str, list[Profile]] = {}
    for p in items:
        by_id.setdefault(p.id, []).append(p)
    dups = {k: v for k, v in by_id.items() if len(v) > 1}
    return items, dups


def get(pid: str) -> Profile:
    items, dups = load_all()
    if pid in dups:
        raise DuplicateError(pid, [p.slug for p in dups[pid]])
    for p in items:
        if p.id == pid:
            return p
    raise KeyError(pid)


def get_by_slug(slug: str) -> Profile:
    p = _load(profiles_dir() / slug)
    if not p:
        raise KeyError(slug)
    return p


class DuplicateError(Exception):
    def __init__(self, pid: str, slugs: list[str]):
        super().__init__(f"у профиля {pid} есть копии: {', '.join(slugs)} — выбери, перенос это или новый профиль")
        self.pid = pid
        self.slugs = slugs


def _unique_dir(name: str) -> Path:
    root = profiles_dir()
    root.mkdir(parents=True, exist_ok=True)
    base = slugify(name)
    d, i = root / base, 2
    while d.exists():
        d, i = root / f"{base}-{i}", i + 1
    return d


def create(name: str, *, group: str = "", proxy: str = "", start_urls: list[str] | None = None,
           notes: str = "", country: str | None = None, locale: str | None = None,
           fp: dict | None = None) -> Profile:
    d = _unique_dir(name)
    (d / "data").mkdir(parents=True)
    p = Profile(d, migrate({
        "schema": SCHEMA,
        "id": new_id(),
        "name": name.strip() or d.name,
        "group": group.strip(),
        "notes": notes,
        "tags": [],
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "proxy": proxy.strip(),
        "fingerprint": fp or fingerprint.generate(country, locale),
        "start_urls": [u.strip() for u in (start_urls or []) if u.strip()],
        "engine": {},
        "agent_access": False,
        "machine": {},
    }))
    p.save()
    return p


def update(pid: str, fields: dict) -> Profile:
    p = get(pid)
    for k, v in fields.items():
        if k not in EDITABLE:
            raise ValueError(f"поле {k} нельзя менять")
        if k == "start_urls":
            v = [u.strip() for u in v if u.strip()]
        p.data[k] = v
    p.save()
    return p


def delete(pid: str) -> Path:
    """Переносит папку профиля в backups/ (не удаляет насовсем)."""
    p = get(pid)
    backups_dir().mkdir(parents=True, exist_ok=True)
    dst = backups_dir() / f"{p.slug}-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.move(str(p.dir), dst)
    shutil.rmtree(cache_dir(pid), ignore_errors=True)
    return dst


def _strip_identity(d: Path) -> None:
    for rel in IDENTITY_FILES:
        t = d / rel
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
        else:
            t.unlink(missing_ok=True)


def new_identity(p: Profile, name: str | None = None) -> Profile:
    """Превращает копию в самостоятельный профиль: новый id и отпечаток, без кук и истории."""
    _strip_identity(p.dir)
    p.data["id"] = new_id()
    p.data["fingerprint"] = fingerprint.regenerate_identity(p.data["fingerprint"])
    p.data["created"] = datetime.now().astimezone().isoformat(timespec="seconds")
    p.data["machine"] = {}
    if name:
        p.data["name"] = name
    p.save()
    return p


def clone(pid: str, name: str | None = None) -> Profile:
    src = get(pid)
    new_name = name or f"{src.data['name']} (копия)"
    d = _unique_dir(new_name)
    shutil.copytree(src.dir, d, ignore=shutil.ignore_patterns("Singleton*", "lockfile", "*.tmp"))
    p = _load(d)
    assert p is not None
    return new_identity(p, new_name)


def append_launch_log(p: Profile, line: str) -> None:
    with (p.dir / "launch.log").open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{line}\n")


def touch_machine(p: Profile, engine_version: str) -> None:
    p.data["engine"] = {"name": "fingerprint-chromium", "last_version": engine_version}
    p.data["machine"] = {"last_host": platform.node(),
                         "last_launch": datetime.now().astimezone().isoformat(timespec="seconds")}
    p.save()
