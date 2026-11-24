"""Подбор отпечатка, не совпадающего с другими профилями (ARCHITECTURE.md §4.3, §4.6).

Значения audio/canvas зависят от компьютера (видеокарта, драйвер), поэтому сравниваются
только пробы, снятые на этом же компьютере этой же версией движка.
"""

from __future__ import annotations

import json
import logging
import platform
import threading
from pathlib import Path

from .. import paths, settings
from . import engine, fingerprint, profiles
from .probe import probe

log = logging.getLogger("xlauncher.uniqueness")

ATTEMPTS = 6
# Веса совпадений: canvas различается почти всегда — его совпадение серьёзнее.
W_AUDIO, W_CANVAS, W_BASE = 1, 3, 4


def _host() -> str:
    return platform.node()


def _version() -> str:
    return settings.load()["engine_version"]


def _comparable(pr: dict | None, version: str) -> bool:
    return bool(pr) and pr.get("engine_version") == version and pr.get("host") == _host() \
        and "err" not in (pr.get("audio"), pr.get("canvas"))


def baseline(exe: Path, version: str) -> dict:
    """Значения браузера без подмены на этом компьютере (кэшируются)."""
    f = paths.root() / "cache" / f"probe-baseline-{version}-{_host()}.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    b = probe(exe, None)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(b), encoding="utf-8")
    return b


def others(exclude_id: str | None, version: str) -> list[dict]:
    items, _ = profiles.load_all()
    return [p.data["fingerprint"]["probe"] for p in items
            if p.id != exclude_id and _comparable(p.data.get("fingerprint", {}).get("probe"), version)]


def score(pr: dict, rest: list[dict], base: dict) -> tuple[int, list[str]]:
    reasons = []
    a = sum(o["audio"] == pr["audio"] for o in rest)
    c = sum(o["canvas"] == pr["canvas"] for o in rest)
    s = a * W_AUDIO + c * W_CANVAS
    if a:
        reasons.append(f"audio совпадает с {a} проф.")
    if c:
        reasons.append(f"canvas совпадает с {c} проф.")
    if pr["audio"] == base.get("audio") or pr["canvas"] == base.get("canvas"):
        s += W_BASE
        reasons.append("совпадает с браузером без подмены")
    return s, reasons


def _probe_fp(exe: Path, fp: dict, version: str) -> dict:
    pr = probe(exe, fp["seed"], fp["hardware_concurrency"])
    pr.update(engine_version=version, host=_host())
    return pr


def pick(template: dict, exclude_id: str | None = None, attempts: int = ATTEMPTS) -> tuple[dict, dict]:
    """Перебирает seed, пока проба не перестанет совпадать с другими. Возвращает (отпечаток, отчёт).

    Если движок не установлен — отпечаток без пробы и отчёт с причиной.
    """
    version = _version()
    exe = engine.find_exe(version)
    if not exe:
        return template, {"checked": False, "reason": f"движок {version} не установлен — проверка пропущена"}
    base = baseline(exe, version)
    rest = others(exclude_id, version)
    best: tuple[int, dict, list[str]] | None = None
    tried = 0
    fp = template
    for i in range(attempts):
        if i:
            fp = fingerprint.regenerate_identity(template)
        tried += 1
        try:
            pr = _probe_fp(exe, fp, version)
        except Exception as e:  # noqa: BLE001
            log.warning("проба не удалась: %s", e)
            continue
        s, reasons = score(pr, rest, base)
        cand = dict(fp, probe=pr)
        if best is None or s < best[0]:
            best = (s, cand, reasons)
        if s == 0:
            break
    if best is None:
        return template, {"checked": False, "reason": "проба движком не удалась", "tried": tried}
    return best[1], {"checked": True, "unique": best[0] == 0, "issues": best[2], "tried": tried,
                     "compared_with": len(rest)}


def ensure_unique(p: profiles.Profile) -> dict:
    """Для только что созданной копии/клона: подобрать отпечаток, не совпадающий с остальными."""
    fp, report = pick(p.data["fingerprint"], exclude_id=p.id)
    p.data["fingerprint"] = fp
    p.save()
    return report


# ---------------------------------------------------------------- проверка всех профилей


class Auditor:
    """Фоновая проба всех профилей без изменения их отпечатков: только отчёт о совпадениях."""

    def __init__(self) -> None:
        self.state: dict = {"status": "idle"}
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self.state.get("status") == "running":
                return
            self.state = {"status": "running", "done": 0, "total": 0}
        threading.Thread(target=self._run, name="fp-audit", daemon=True).start()

    def _run(self) -> None:
        try:
            self.state = {"status": "done", **audit(self._progress)}
        except Exception as e:  # noqa: BLE001
            log.exception("проверка отпечатков")
            self.state = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    def _progress(self, done: int, total: int) -> None:
        self.state.update(done=done, total=total)


auditor = Auditor()


def audit(progress=lambda d, t: None) -> dict:
    version = _version()
    exe = engine.find_exe(version)
    if not exe:
        raise RuntimeError(f"движок {version} не установлен")
    base = baseline(exe, version)
    items, _ = profiles.load_all()
    todo = [p for p in items if not _comparable(p.data.get("fingerprint", {}).get("probe"), version)]
    for i, p in enumerate(todo):
        progress(i, len(todo))
        try:
            p.data["fingerprint"]["probe"] = _probe_fp(exe, p.data["fingerprint"], version)
            p.save()
        except Exception as e:  # noqa: BLE001
            log.warning("проба %s: %s", p.slug, e)
    progress(len(todo), len(todo))
    items, _ = profiles.load_all()
    probes = [(p, p.data["fingerprint"].get("probe")) for p in items]
    probes = [(p, pr) for p, pr in probes if _comparable(pr, version)]
    groups = []
    for key in ("audio", "canvas"):
        by: dict[str, list[str]] = {}
        for p, pr in probes:
            by.setdefault(pr[key], []).append(p.data["name"])
        groups += [{"kind": key, "profiles": names} for v, names in by.items() if len(names) > 1]
    base_hits = [p.data["name"] for p, pr in probes
                 if pr["audio"] == base.get("audio") or pr["canvas"] == base.get("canvas")]
    return {"checked": len(probes), "collisions": groups, "baseline_hits": base_hits}
