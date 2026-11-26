"""Переезд профилей на другой компьютер через свой сервер xl-relay (docs: ARCHITECTURE.md §10).

Ключ переезда  XL1-XXXX-…(32 знака Crockford Base32 = 160 бит)@<адрес релея>:<порт>
  из секрета через HKDF-SHA256 выводятся package_id (имя пакета на сервере), enc_key (AES-256-GCM,
  только у лаунчеров) и delete_token (сервер хранит лишь его sha256).

Открытый текст пакета = заголовок + ZIP:
  b"XLMOVE1\\n" + 8 байт длины (big-endian) + gzip(move-manifest.json) + ZIP с файлами.
  В манифесте — профили и sha256/размер каждого файла: приёмник сверяет всё до подтверждения.

Шифрование — AES-256-GCM фрагментами по 8 МиБ открытого текста:
  nonce = 4 нулевых байта + номер фрагмента (8 байт BE),
  AAD = package_id (16 байт) + номер (8 байт BE) + признак последнего (1 байт) —
  фрагменты нельзя переставить, подменить или обрезать незаметно.

Сервер видит только шифротекст. Удаление на сервере — только confirm (приёмник проверил все файлы)
или cancel (отправитель отменил) с delete_token.

Пока есть run/move/state.json, профили этой машины заморожены: их нельзя запустить.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Callable

from .. import __version__, paths, settings
from . import portability, profiles, runstate

log = logging.getLogger("xlauncher.move")

FORMAT = 1
CHUNK = 8 * 1024 * 1024
MAGIC = b"XLMOVE1\n"
RETRY_DELAYS = (2, 4, 8, 16, 32)
HTTP_TIMEOUT = 120
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
KEY_PREFIX = "XL1"

# Что не переносится внутри папки профиля (по имени файла/папки на любом уровне).
EXCLUDE_NAMES = {
    "Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "GraphiteDawnCache",
    "ShaderCache", "GrShaderCache", "component_crx_cache", "extensions_crx_cache", "Crashpad",
    "LOCK", "lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket",
}
EXCLUDE_GLOBS = ("optimization_guide_*", "BrowserMetrics*", "*.tmp", "Singleton*")
EXCLUDE_PATHS = (("Service Worker", "CacheStorage"), ("Service Worker", "ScriptCache"))
# Настройки, которые у каждой машины свои.
LOCAL_SETTINGS = ("remote", "move")

Progress = Callable[..., None]


class MoveError(Exception):
    """Понятная пользователю ошибка переезда."""


class Cancelled(MoveError):
    def __init__(self) -> None:
        super().__init__("отменено")


# ---------------------------------------------------------------- ключ


def _b32(data: bytes) -> str:
    n, bits = int.from_bytes(data, "big"), len(data) * 8
    return "".join(CROCKFORD[(n >> (bits - 5 * (i + 1))) & 31] for i in range(bits // 5))


def _unb32(text: str) -> bytes:
    n = 0
    for ch in text:
        n = (n << 5) | CROCKFORD.index(ch)
    return n.to_bytes(len(text) * 5 // 8, "big")


RELAY_RX = re.compile(r"^(?:[a-z0-9]([a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*|\[[0-9a-f:]+\]):\d{1,5}$")


def normalize_relay(relay: str) -> str:
    r = "".join((relay or "").split()).lower()
    r = re.sub(r"^https?://", "", r).rstrip("/")
    if not RELAY_RX.match(r) or not 0 < int(r.rsplit(":", 1)[1]) < 65536:
        raise MoveError("адрес сервера переезда — в виде имя:порт или 100.x.x.x:порт")
    return r


def format_key(secret: bytes, relay: str) -> str:
    if len(secret) != 20:
        raise ValueError("секрет — 20 байт")
    body = _b32(secret)
    return f"{KEY_PREFIX}-" + "-".join(body[i:i + 4] for i in range(0, 32, 4)) + "@" + normalize_relay(relay)


def parse_key(text: str) -> tuple[bytes, str]:
    """Ключ из ввода человека: регистр, пробелы, переносы, O/0 и I/L/1 прощаются."""
    t = "".join((text or "").split())
    if "@" not in t:
        raise MoveError("в ключе нет адреса сервера (часть после @)")
    body, relay = t.rsplit("@", 1)
    body = body.upper().replace("-", "")
    if not body.startswith(KEY_PREFIX):
        raise MoveError("это не ключ переезда XLauncher (должен начинаться с XL1-)")
    body = body[len(KEY_PREFIX):].translate(str.maketrans("OIL", "011"))
    if len(body) != 32 or any(c not in CROCKFORD for c in body):
        raise MoveError("ключ повреждён: после XL1 должно быть 32 знака (буквы и цифры)")
    return _unb32(body), normalize_relay(relay)


@dataclass(frozen=True)
class Keys:
    package_id: str
    enc_key: bytes
    delete_token: str

    @property
    def pid_bytes(self) -> bytes:
        return bytes.fromhex(self.package_id)

    @property
    def delete_hash(self) -> str:
        return hashlib.sha256(bytes.fromhex(self.delete_token)).hexdigest()


def derive(secret: bytes) -> Keys:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    def kdf(info: str, n: int) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=n, salt=None, info=info.encode()).derive(secret)

    return Keys(kdf("xl-move v1 id", 16).hex(), kdf("xl-move v1 key", 32), kdf("xl-move v1 delete", 32).hex())


def key_id(key: str) -> str:
    """Последние 4 знака ключа (без адреса) — для подтверждения очистки."""
    return key.split("@", 1)[0].replace("-", "")[-4:]


# ---------------------------------------------------------------- шифрование


def _nonce(n: int) -> bytes:
    return b"\0" * 4 + n.to_bytes(8, "big")


def _aad(keys: Keys, n: int, last: bool) -> bytes:
    return keys.pid_bytes + n.to_bytes(8, "big") + (b"\1" if last else b"\0")


def encrypt_chunk(keys: Keys, n: int, plain: bytes, last: bool) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(keys.enc_key).encrypt(_nonce(n), plain, _aad(keys, n, last))


def decrypt_chunk(keys: Keys, n: int, cipher: bytes, last: bool) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        return AESGCM(keys.enc_key).decrypt(_nonce(n), cipher, _aad(keys, n, last))
    except InvalidTag:
        raise MoveError("ключ не подходит или данные на сервере повреждены") from None


class PlainSource:
    """Открытый текст пакета без лишней копии на диске: заголовок в памяти + ZIP-файл."""

    def __init__(self, header: bytes, zip_path: Path) -> None:
        self.header = header
        self.zip_path = zip_path
        self.size = len(header) + zip_path.stat().st_size

    @property
    def chunks(self) -> int:
        return max(1, -(-self.size // CHUNK))

    def read_chunk(self, n: int) -> bytes:
        start, end = (n - 1) * CHUNK, min(n * CHUNK, self.size)
        out = bytearray()
        if start < len(self.header):
            out += self.header[start:min(end, len(self.header))]
        zstart = max(start - len(self.header), 0)
        zend = end - len(self.header)
        if zend > zstart:
            with open(self.zip_path, "rb") as f:
                f.seek(zstart)
                out += f.read(zend - zstart)
        return bytes(out)

    def cipher_chunk(self, keys: Keys, n: int) -> bytes:
        return encrypt_chunk(keys, n, self.read_chunk(n), n == self.chunks)


def make_header(manifest: dict) -> bytes:
    body = gzip.compress(json.dumps(manifest, ensure_ascii=False).encode(), mtime=0)
    return MAGIC + len(body).to_bytes(8, "big") + body


def read_header(data: bytes) -> tuple[dict, int] | None:
    """(манифест, длина заголовка) или None, если данных пока мало."""
    if len(data) < len(MAGIC) + 8:
        return None
    if not data.startswith(MAGIC):
        raise MoveError("это не пакет переезда XLauncher")
    n = int.from_bytes(data[len(MAGIC):len(MAGIC) + 8], "big")
    end = len(MAGIC) + 8 + n
    if len(data) < end:
        return None
    try:
        return json.loads(gzip.decompress(data[len(MAGIC) + 8:end])), end
    except (OSError, ValueError):
        raise MoveError("оглавление пакета повреждено") from None


# ---------------------------------------------------------------- что переносится


def _excluded(rel: tuple[str, ...]) -> bool:
    for part in rel:
        if part in EXCLUDE_NAMES or any(fnmatch(part, g) for g in EXCLUDE_GLOBS):
            return True
    return any(rel[i:i + len(p)] == p for p in EXCLUDE_PATHS for i in range(len(rel) - len(p) + 1))


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def collect(profile_ids: list[str] | None = None) -> tuple[list[profiles.Profile], list[tuple[Path, str]]]:
    """Профили и файлы для переноса: [(путь на диске, путь в архиве)]."""
    items, dups = profiles.load_all()
    if dups:
        raise MoveError("есть профили с одинаковым id — сначала разберитесь с дублями")
    if profile_ids is not None:
        items = [p for p in items if p.id in set(profile_ids)]
    files: list[tuple[Path, str]] = []
    for p in items:
        for dirpath, dirnames, filenames in os.walk(p.dir):
            rel_dir = Path(dirpath).relative_to(p.dir).parts
            dirnames[:] = sorted(d for d in dirnames if not _excluded((*rel_dir, d)))
            for fn in sorted(filenames):
                if not _excluded((*rel_dir, fn)):
                    files.append((Path(dirpath) / fn, "/".join(("profiles", p.slug, *rel_dir, fn))))
    root = paths.root()
    if (root / "warmup" / "sites.json").is_file():
        files.append((root / "warmup" / "sites.json", "warmup/sites.json"))
    return items, files


def portable_settings() -> dict:
    s = {}
    try:
        s = json.loads((paths.root() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {k: v for k, v in s.items() if k not in LOCAL_SETTINGS}


def estimate(profile_ids: list[str] | None = None) -> dict:
    items, files = collect(profile_ids)
    size = 0
    for p, _ in files:
        try:
            size += p.stat().st_size
        except OSError:
            pass
    return {"profiles": len(items), "files": len(files), "bytes": size}


def pack(out_dir: Path, profile_ids: list[str] | None = None, progress: Progress | None = None,
         cancel: threading.Event | None = None) -> tuple[Path, dict]:
    """ZIP с файлами и манифест. Занятый файл — понятная ошибка с именем профиля."""
    items, files = collect(profile_ids)
    if not items:
        raise MoveError("на этом компьютере нет профилей для переезда")
    out_dir.mkdir(parents=True, exist_ok=True)
    zpath = out_dir / "package.zip"
    by_slug = {p.slug: p for p in items}
    man_files, per_profile = [], {p.slug: {"files": 0, "bytes": 0} for p in items}
    total = len(files) + 1
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=5, allowZip64=True) as z:
        sdata = json.dumps(portable_settings(), ensure_ascii=False, indent=2).encode()
        z.writestr("settings.json", sdata)
        man_files.append({"path": "settings.json", "size": len(sdata), "sha256": hashlib.sha256(sdata).hexdigest()})
        for i, (src, arc) in enumerate(files, 1):
            if cancel and cancel.is_set():
                raise Cancelled()
            try:
                digest, size = _sha256_file(src), src.stat().st_size
                z.write(src, arc)
            except FileNotFoundError:
                continue  # исчез между обходом и упаковкой (временный файл браузера)
            except OSError as e:
                slug = arc.split("/")[1] if arc.startswith("profiles/") else ""
                who = f"профиль «{by_slug[slug].data.get('name')}»" if slug in by_slug else arc
                raise MoveError(f"не удалось прочитать файл ({who}: {arc.split('/', 2)[-1]}): {e.strerror or e}. "
                                "Закройте профиль и повторите") from None
            man_files.append({"path": arc, "size": size, "sha256": digest})
            if arc.startswith("profiles/"):
                pp = per_profile[arc.split("/")[1]]
                pp["files"] += 1
                pp["bytes"] += size
            if progress and (i % 25 == 0 or i == total - 1):
                progress("pack", i + 1, total)
    manifest = {
        "format": FORMAT,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_host": socket.gethostname(),
        "launcher_version": __version__,
        "engine_version": settings.load().get("engine_version"),
        "profiles": [{"id": p.id, "name": p.data.get("name"), "slug": p.slug, "group": p.data.get("group", ""),
                      **per_profile[p.slug]} for p in items],
        "files": man_files,
    }
    return zpath, manifest


# ---------------------------------------------------------------- клиент релея


class Relay:
    def __init__(self, relay: str, timeout: float = HTTP_TIMEOUT) -> None:
        import httpx

        self.addr = normalize_relay(relay)
        # trust_env=False: системный прокси не должен перехватывать адрес внутри Tailscale
        self.http = httpx.Client(base_url=f"http://{self.addr}/v1", timeout=timeout, trust_env=False)

    def close(self) -> None:
        self.http.close()

    def _call(self, method: str, url: str, *, retry: bool = True, ok=(200, 201), cancel=None, **kw):
        import httpx

        delays = (*RETRY_DELAYS, None) if retry else (None,)
        for delay in delays:
            try:
                r = self.http.request(method, url, **kw)
            except httpx.HTTPError as e:
                if delay is None:
                    raise MoveError(f"нет связи с сервером переезда {self.addr}: {type(e).__name__}") from None
                if cancel and cancel.wait(delay):
                    raise Cancelled() from None
                if not cancel:
                    time.sleep(delay)
                continue
            if r.status_code in ok or r.status_code < 500 or delay is None:
                return r
            if cancel and cancel.wait(delay):
                raise Cancelled()
            if not cancel:
                time.sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _err(r) -> str:
        try:
            d = r.json()
            e = d.get("error") if isinstance(d, dict) else d
            return e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)
        except ValueError:
            return r.text[:200]

    def health(self) -> dict:
        r = self._call("GET", "/health", retry=False)
        if r.status_code != 200:
            raise MoveError(f"сервер переезда ответил {r.status_code}")
        return r.json()

    def status(self, pid: str):
        return self._call("GET", f"/packages/{pid}")

    def create(self, pid: str, body: dict) -> None:
        r = self._call("POST", f"/packages/{pid}", json=body)
        if r.status_code == 507:
            raise MoveError("на сервере переезда не хватает места")
        if r.status_code not in (201, 409):
            raise MoveError(f"сервер не принял пакет: {self._err(r)}")

    def put_chunk(self, pid: str, n: int, data: bytes, cancel=None) -> None:
        r = self._call("PUT", f"/packages/{pid}/chunks/{n}", content=data, cancel=cancel,
                       headers={"X-Chunk-Sha256": hashlib.sha256(data).hexdigest(),
                                "Content-Type": "application/octet-stream"})
        if r.status_code != 200:
            raise MoveError(f"фрагмент {n} не принят: {self._err(r)}")

    def complete(self, pid: str) -> list[str]:
        r = self._call("POST", f"/packages/{pid}/complete")
        if r.status_code != 200:
            raise MoveError(f"сервер не подтвердил загрузку: {self._err(r)}")
        return r.json()["chunk_sha256"]

    def get_chunk(self, pid: str, n: int, cancel=None) -> bytes:
        r = self._call("GET", f"/packages/{pid}/chunks/{n}", cancel=cancel)
        if r.status_code != 200:
            raise MoveError(f"фрагмент {n} не скачался: {self._err(r)}")
        return r.content

    def finish(self, pid: str, token: str, what: str) -> str:
        """confirm или cancel; возвращает статус ("confirmed"/"cancelled"/"gone")."""
        r = self._call("POST", f"/packages/{pid}/{what}", json={"delete_token": token})
        if r.status_code == 200:
            return r.json().get("status", what)
        if r.status_code == 410:  # пакет уже удалён: confirmed / cancelled / removed
            try:
                return r.json().get("status") or "gone"
            except ValueError:
                return "gone"
        if r.status_code == 404:
            return "gone"
        raise MoveError(f"сервер не выполнил {what}: {self._err(r)}")


# ---------------------------------------------------------------- состояние отправки


def move_dir() -> Path:
    return paths.run_dir() / "move"


def state_path() -> Path:
    return move_dir() / "state.json"


def move_txt() -> Path:
    return paths.root() / "MOVE.txt"


def load_state() -> dict | None:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_state(st: dict) -> None:
    move_dir().mkdir(parents=True, exist_ok=True)
    paths.write_atomic(state_path(), json.dumps(st, ensure_ascii=False, indent=2))


FROZEN_TEXT = ("Профили переданы на другой компьютер (ключ в MOVE.txt). "
               "Отмените переезд, чтобы работать здесь")


def frozen_message() -> str | None:
    """Текст ошибки запуска, если профили этой машины заморожены переездом."""
    return FROZEN_TEXT if state_path().exists() else None


def _write_move_txt(st: dict) -> None:
    names = ", ".join(p["name"] or p["id"] for p in st["profiles"][:20])
    more = f" и ещё {len(st['profiles']) - 20}" if len(st["profiles"]) > 20 else ""
    dt = datetime.fromisoformat(st["created"]).strftime("%d.%m.%Y %H:%M")
    text = (f"XLauncher — переезд\r\nДата: {dt}\r\nС компьютера: {st['source_host']}\r\n"
            f"Профилей: {len(st['profiles'])} ({names}{more})\r\nКлюч: {st['key']}\r\n\r\n"
            "На новом компьютере: запустите xlauncher.exe → «Переезд» → «Принять» → вставьте ключ.\r\n"
            "Ключ — доступ ко всем аккаунтам этих профилей. После приёма удалите этот файл.\r\n")
    move_txt().write_text(text, encoding="utf-8-sig")


def _check_all_stopped(profile_ids: list[str] | None = None) -> None:
    items, _ = profiles.load_all()
    running = [p.data.get("name") or p.id for p in items
               if (profile_ids is None or p.id in profile_ids) and runstate.is_active(p.id)]
    if running:
        raise MoveError("сначала остановите профили: " + ", ".join(running[:5]) + ("…" if len(running) > 5 else ""))


def export(relay: str, profile_ids: list[str] | None = None, progress: Progress | None = None,
           cancel: threading.Event | None = None, pull: dict | None = None) -> dict:
    """Упаковать, зашифровать, загрузить на релей. Возвращает состояние (ключ — в st['key'])."""
    if load_state():
        raise MoveError("уже есть незавершённый переезд — продолжите или отмените его")
    if pending_imports():
        raise MoveError("на этот компьютер только что приняты профили, и сервер ещё не подтвердил удаление пакета")
    relay = normalize_relay(relay)
    _check_all_stopped(profile_ids)
    cl = Relay(relay)
    try:
        cl.health()
    finally:
        cl.close()
    items, _ = profiles.load_all()
    for p in items:
        if profile_ids is None or p.id in profile_ids:
            try:
                portability.save_key(p.dir)  # без этого куки не расшифруются под другим пользователем Windows
            except OSError:
                pass
    out = move_dir() / "out"
    shutil.rmtree(out, ignore_errors=True)
    try:
        zpath, manifest = pack(out, profile_ids, progress, cancel)
        header = make_header(manifest)
        (out / "header.bin").write_bytes(header)
        src = PlainSource(header, zpath)
        secret = secrets.token_bytes(20)
        keys = derive(secret)
        # Первый проход: хеши шифротекста (сервер проверяет их при приёме и в complete).
        total, sums, h = 0, [], hashlib.sha256()
        for n in range(1, src.chunks + 1):
            if cancel and cancel.is_set():
                raise Cancelled()
            c = src.cipher_chunk(keys, n)
            sums.append(hashlib.sha256(c).hexdigest())
            h.update(c)
            total += len(c)
            if progress:
                progress("encrypt", n, src.chunks)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    st = {
        "format": FORMAT, "phase": "uploading", "secret": secret.hex(), "key": format_key(secret, relay),
        "relay": relay, "package_id": keys.package_id, "created": manifest["created"],
        "source_host": manifest["source_host"], "chunks": src.chunks, "cipher_bytes": total,
        "cipher_sha256": h.hexdigest(), "chunk_sha256": sums,
        "profiles": [{"id": p["id"], "name": p["name"]} for p in manifest["profiles"]],
        "plain_bytes": src.size,
    }
    if pull:
        st["pull"] = pull  # профили забирает другая машина связки: ключ ей, MOVE.txt не нужен
    save_state(st)  # с этого момента профили заморожены
    return upload(progress, cancel)


def upload(progress: Progress | None = None, cancel: threading.Event | None = None) -> dict:
    """Загрузка (и докачка) пакета из state.json: досылаются только недостающие фрагменты."""
    st = load_state()
    if not st:
        raise MoveError("нет переезда для продолжения")
    if st["phase"] == "sent":
        return st
    keys = derive(bytes.fromhex(st["secret"]))
    out = move_dir() / "out"
    try:
        src = PlainSource((out / "header.bin").read_bytes(), out / "package.zip")
    except OSError:
        raise MoveError("временный архив переезда пропал — отмените переезд и начните заново") from None
    if src.chunks != st["chunks"]:
        raise MoveError("временный архив переезда изменился — отмените переезд и начните заново")
    pid = keys.package_id
    cl = Relay(st["relay"])
    try:
        r = cl.status(pid)
        if r.status_code == 404:
            cl.create(pid, {"chunks": st["chunks"], "cipher_bytes": st["cipher_bytes"],
                            "cipher_sha256": st["cipher_sha256"], "delete_hash": keys.delete_hash,
                            "source_host": st["source_host"], "profiles": len(st["profiles"]),
                            "created": st["created"]})
            have: set[int] = set()
            complete = False
        elif r.status_code == 200:
            have, complete = set(r.json().get("received") or []), bool(r.json().get("complete"))
        else:
            raise MoveError(f"пакет на сервере недоступен: {cl._err(r)}")
        started, sent_bytes = time.time(), 0
        if not complete:
            for n in range(1, st["chunks"] + 1):
                if n in have:
                    continue
                if cancel and cancel.is_set():
                    raise Cancelled()
                c = src.cipher_chunk(keys, n)
                if hashlib.sha256(c).hexdigest() != st["chunk_sha256"][n - 1]:
                    raise MoveError("временный архив переезда изменился — отмените переезд и начните заново")
                cl.put_chunk(pid, n, c, cancel)
                sent_bytes += len(c)
                if progress:
                    done = len(have) + 1
                    have.add(n)
                    progress("upload", done, st["chunks"], bytes=sent_bytes, seconds=time.time() - started,
                             total_bytes=st["cipher_bytes"])
        server_sums = cl.complete(pid)
    finally:
        cl.close()
    if server_sums != st["chunk_sha256"]:
        raise MoveError("хеши на сервере не совпали с отправленными — данные не считаются отправленными")
    st.update(phase="sent", sent=datetime.now().astimezone().isoformat(timespec="seconds"))
    save_state(st)
    if not st.get("pull"):
        _write_move_txt(st)
    shutil.rmtree(out, ignore_errors=True)
    log.info("переезд отправлен: %s профилей, пакет %s…", len(st["profiles"]), pid[:8])
    return st


def cancel_export() -> dict:
    """Отменить переезд: удалить пакет на сервере, разморозить профили."""
    st = load_state()
    if not st:
        return {"ok": True, "status": "none"}
    keys = derive(bytes.fromhex(st["secret"]))
    cl = Relay(st["relay"])
    try:
        status = cl.finish(keys.package_id, keys.delete_token, "cancel")
    finally:
        cl.close()
    if status == "confirmed":
        raise MoveError("пакет уже принят на другом компьютере — отменять нечего, профили там")
    shutil.rmtree(move_dir() / "out", ignore_errors=True)
    state_path().unlink(missing_ok=True)
    move_txt().unlink(missing_ok=True)
    log.info("переезд отменён")
    return {"ok": True, "status": status}


def finish(confirm: str | None = None, auto: bool = False) -> dict:
    """Очистить этот компьютер после переезда: приложение выходит с кодом 76, загрузчик удаляет папку.

    Вручную (push) — по последним 4 знакам ключа. Автоматически (pull) — только когда сервер ответил,
    что другая машина всё скачала, проверила и подтвердила."""
    from .. import lifecycle

    st = load_state()
    if not st or st.get("phase") != "sent":
        raise MoveError("нечего завершать: переезд не отправлен")
    if auto:
        if remote_status() != "confirmed":
            raise MoveError("другая машина ещё не подтвердила приём")
    elif (confirm or "").strip().upper() != key_id(st["key"]):
        raise MoveError("последние 4 знака ключа не совпали")
    if lifecycle.bootstrap_version() < 5:
        raise MoveError("загрузчик устарел для очистки — перезапустите XLauncher (он обновится сам) и повторите")
    items, _ = profiles.load_all()
    running = [p.data.get("name") or p.id for p in items if runstate.is_active(p.id)]
    if running:
        raise MoveError("запущены профили: " + ", ".join(running[:5]))
    from . import policies, remote

    try:
        remote.server.stop()
    except Exception:  # noqa: BLE001
        log.exception("остановка удалённого доступа")
    if sys.platform == "win32":
        from . import service

        for fn in (lambda: service.autostart_set(False), policies.remove):
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("очистка реестра")
    keep = ["xlauncher.exe"] if st.get("pull") else ["xlauncher.exe", "MOVE.txt"]
    paths.write_atomic(paths.run_dir() / "wipe-request.json",
                       json.dumps({"keep": keep, "package": st["package_id"][:8], "time": time.time()}))
    log.warning("очистка компьютера после переезда: приложение выходит, загрузчик удаляет данные")
    threading.Timer(1.0, lifecycle.request_exit, args=(lifecycle.EXIT_WIPE,)).start()  # ответ API успеет уйти
    return {"ok": True}


def remote_status() -> str | None:
    """Что с отправленным пакетом на сервере: waiting | confirmed | cancelled | removed | None (нет связи)."""
    st = load_state()
    if not st:
        return None
    cl = Relay(st["relay"], timeout=10)
    try:
        r = cl._call("GET", f"/packages/{derive(bytes.fromhex(st['secret'])).package_id}", retry=False)
    except MoveError:
        return None
    finally:
        cl.close()
    if r.status_code == 410:
        return (r.json() or {}).get("status") or "removed"
    return "waiting" if r.status_code == 200 else None


# ---------------------------------------------------------------- приём


def _in_dir(pid: str) -> Path:
    return move_dir() / "in" / pid


def _pending_dir() -> Path:
    return move_dir() / "pending-confirm"


def pending_imports() -> list[dict]:
    out = []
    for f in sorted(_pending_dir().glob("*.json")) if _pending_dir().is_dir() else []:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return out


def _open_package(key: str) -> tuple[Keys, str, Relay, dict]:
    secret, relay = parse_key(key)
    keys = derive(secret)
    cl = Relay(relay)
    try:
        cl.health()
        r = cl.status(keys.package_id)
    except BaseException:
        cl.close()
        raise
    if r.status_code == 404:
        cl.close()
        raise MoveError("пакет не найден: ключ неверный или переезд уже принят/отменён")
    if r.status_code == 410:
        cl.close()
        st = (r.json() or {}).get("status")
        raise MoveError({"confirmed": "этот переезд уже принят", "cancelled": "переезд отменён на исходном компьютере"}
                        .get(st, "пакет удалён с сервера"))
    meta = r.json()
    if r.status_code != 200 or not meta.get("complete"):
        cl.close()
        raise MoveError("отправка ещё не закончена на исходном компьютере")
    return keys, relay, cl, meta


def _chunk_plain(cl: Relay, keys: Keys, meta: dict, n: int, cancel=None) -> bytes:
    c = cl.get_chunk(keys.package_id, n, cancel)
    want = (meta.get("chunk_sha256") or {}).get(str(n))
    if want and hashlib.sha256(c).hexdigest() != want:
        raise MoveError(f"фрагмент {n} скачался с ошибкой")
    return decrypt_chunk(keys, n, c, n == meta["chunks"])


def _conflicts(manifest: dict) -> list[dict]:
    items, _ = profiles.load_all()
    local = {p.id: p for p in items}
    return [{"id": p["id"], "name": p["name"], "local_name": local[p["id"]].data.get("name")}
            for p in manifest["profiles"] if p["id"] in local]


def inspect(key: str) -> dict:
    """Сводка пакета по ключу: откуда, сколько, какие профили, совпадения с этой машиной."""
    keys, relay, cl, meta = _open_package(key)
    try:
        buf, n = b"", 0
        while (hdr := read_header(buf)) is None:
            n += 1
            if n > meta["chunks"]:
                raise MoveError("оглавление пакета повреждено")
            buf += _chunk_plain(cl, keys, meta, n)
    finally:
        cl.close()
    manifest = hdr[0]
    if manifest.get("format") != FORMAT:
        raise MoveError("пакет сделан другой версией XLauncher — обновите лаунчер")
    return {"package_id": keys.package_id, "relay": relay, "source_host": manifest["source_host"],
            "created": manifest["created"], "launcher_version": manifest.get("launcher_version"),
            "profiles": manifest["profiles"], "bytes": sum(f["size"] for f in manifest["files"]),
            "cipher_bytes": meta["cipher_bytes"], "conflicts": _conflicts(manifest)}


def _safe_name(name: str) -> PurePosixPath:
    p = PurePosixPath(name.replace("\\", "/"))
    if (p.is_absolute() or ".." in p.parts or not p.parts or ":" in p.parts[0]
            or not (p.parts[0] == "profiles" and len(p.parts) >= 3 or str(p) in ("settings.json", "warmup/sites.json"))):
        raise MoveError(f"в пакете недопустимый путь: {name}")
    return p


def download(key: str, progress: Progress | None = None, cancel: threading.Event | None = None) -> tuple[Path, dict]:
    """Скачать и расшифровать все фрагменты (с докачкой). Возвращает (package.bin, meta)."""
    keys, relay, cl, meta = _open_package(key)
    d = _in_dir(keys.package_id)
    d.mkdir(parents=True, exist_ok=True)
    plain, done_f = d / "package.bin", d / "done.json"
    try:
        done = set(json.loads(done_f.read_text()))
    except (OSError, ValueError):
        done = set()
    if not plain.exists():
        done = set()
        plain.touch()
    started, got = time.time(), 0
    try:
        with open(plain, "r+b") as f:
            for n in range(1, meta["chunks"] + 1):
                if n in done:
                    continue
                if cancel and cancel.is_set():
                    raise Cancelled()
                p = _chunk_plain(cl, keys, meta, n, cancel)
                if n < meta["chunks"] and len(p) != CHUNK:
                    raise MoveError(f"фрагмент {n} неполный")
                f.seek((n - 1) * CHUNK)
                f.write(p)
                if n == meta["chunks"]:
                    f.truncate((n - 1) * CHUNK + len(p))
                f.flush()
                done.add(n)
                paths.write_atomic(done_f, json.dumps(sorted(done)))
                got += CHUNK
                if progress:
                    progress("download", len(done), meta["chunks"], bytes=got, seconds=time.time() - started,
                             total_bytes=meta["cipher_bytes"])
    finally:
        cl.close()
    return plain, meta


def verify(plain: Path, stage: Path, progress: Progress | None = None) -> dict:
    """Распаковать в stage и сверить каждый файл с манифестом. Любое расхождение — ошибка."""
    with open(plain, "rb") as f:
        head = f.read(len(MAGIC) + 8)
        if len(head) == len(MAGIC) + 8 and head.startswith(MAGIC):
            head += f.read(int.from_bytes(head[len(MAGIC):], "big"))
        hdr = read_header(head)
    if not hdr:
        raise MoveError("пакет обрезан")
    manifest, _ = hdr
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    want = {f["path"]: f for f in manifest["files"]}
    try:
        z = zipfile.ZipFile(plain)
    except zipfile.BadZipFile:
        raise MoveError("архив в пакете повреждён") from None
    with z:
        names = [i for i in z.infolist() if not i.is_dir()]
        extra = sorted({i.filename for i in names} - set(want))
        missing = sorted(set(want) - {i.filename for i in names})
        if extra or missing:
            raise MoveError("состав архива не совпал с оглавлением: "
                            + ", ".join([f"лишний {x}" for x in extra[:3]] + [f"нет {x}" for x in missing[:3]]))
        root = stage.resolve()
        for i, info in enumerate(names, 1):
            rel = _safe_name(info.filename)
            dst = stage.joinpath(*rel.parts)
            if root not in dst.resolve().parents:
                raise MoveError(f"в пакете недопустимый путь: {info.filename}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            h, size = hashlib.sha256(), 0
            with z.open(info) as src, open(dst, "wb") as out:
                while block := src.read(1 << 20):
                    h.update(block)
                    size += len(block)
                    out.write(block)
            w = want[info.filename]
            if h.hexdigest() != w["sha256"] or size != w["size"]:
                raise MoveError(f"файл не совпал с оригиналом: {info.filename}")
            if progress and (i % 25 == 0 or i == len(names)):
                progress("verify", i, len(names))
    warnings = []
    for p in manifest["profiles"]:
        d = stage / "profiles" / p["slug"]
        try:
            data = json.loads((d / "profile.json").read_text(encoding="utf-8"))
            if data.get("id") != p["id"]:
                raise ValueError
        except (OSError, ValueError):
            raise MoveError(f"профиль «{p['name']}» в пакете не читается") from None
        if (d / "data" / "Local State").exists() and not (d / portability.KEY_FILE).exists():
            warnings.append(f"«{p['name']}»: нет резервной копии ключа кук — входы на сайты могут слететь")
    return {"manifest": manifest, "warnings": warnings}


def _unique_slug(slug: str) -> str:
    base, i = slug, 2
    while (profiles.profiles_dir() / slug).exists():
        slug, i = f"{base}-{i}", i + 1
    return slug


def install(stage: Path, manifest: dict, conflicts: dict[str, str] | None = None,
            backup_existing: bool = False) -> dict:
    """Перенести проверенные профили из stage. conflicts: {id: "replace"|"skip"} (по умолчанию skip).
    backup_existing — все профили этой машины сначала уходят в backups/<дата>-before-move/ (режим «Забрать сюда»)."""
    conflicts = conflicts or {}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = profiles.backups_dir() / f"{stamp}-before-move"
    pdir = profiles.profiles_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    items, _ = profiles.load_all()
    local = {p.id: p for p in items}
    moved_away: list[str] = []

    def backup(p: profiles.Profile) -> None:
        bdir.mkdir(parents=True, exist_ok=True)
        os.replace(p.dir, bdir / p.slug)
        moved_away.append(p.slug)

    if backup_existing:
        for p in items:
            if runstate.is_active(p.id):
                raise MoveError(f"профиль «{p.data.get('name')}» запущен — остановите его")
        for p in items:
            backup(p)
        if (paths.root() / "settings.json").exists():
            bdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(paths.root() / "settings.json", bdir / "settings.json")
        local = {}
    host = manifest.get("source_host") or "другого компьютера"
    when = datetime.now().strftime("%d.%m.%Y %H:%M")
    added, replaced, skipped = [], [], []
    for p in manifest["profiles"]:
        if p["id"] in local:
            if conflicts.get(p["id"]) != "replace":
                skipped.append(p["name"])
                continue
            if runstate.is_active(p["id"]):
                raise MoveError(f"профиль «{local[p['id']].data.get('name')}» запущен — остановите его")
            backup(local[p["id"]])
            replaced.append(p["name"])
        else:
            added.append(p["name"])
        slug = _unique_slug(p["slug"])
        os.replace(stage / "profiles" / p["slug"], pdir / slug)
        prof = profiles.get_by_slug(slug)
        profiles.append_launch_log(prof, f"Перенесён с {host} {when}")
    # Настройки: переносимые поля — из пакета, свои remote/move — остаются.
    try:
        incoming = json.loads((stage / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        incoming = {}
    incoming = {k: v for k, v in incoming.items() if k not in LOCAL_SETTINGS}
    if incoming:
        settings.save(incoming)
    sites = stage / "warmup" / "sites.json"
    if sites.exists():
        dst = paths.root() / "warmup" / "sites.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.read_bytes() != sites.read_bytes():
            bdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, bdir / "sites.json")
        os.replace(sites, dst)
    return {"added": added, "replaced": replaced, "skipped": skipped,
            "backup": str(bdir) if bdir.exists() else None}


def receive(key: str, conflicts: dict[str, str] | None = None, backup_existing: bool = False,
            progress: Progress | None = None, cancel: threading.Event | None = None) -> dict:
    """Полный приём: скачать → проверить всё → перенести → подтвердить (сервер удаляет пакет)."""
    if load_state():
        raise MoveError("с этого компьютера идёт свой переезд — отмените его, чтобы принимать профили")
    plain, meta = download(key, progress, cancel)
    keys = derive(parse_key(key)[0])
    d = _in_dir(keys.package_id)
    # Общий хеш шифротекста сверил сервер в complete; здесь каждый фрагмент прошёл GCM с признаком последнего.
    res = verify(plain, d / "stage", progress)
    manifest = res["manifest"]
    out = install(d / "stage", manifest, conflicts, backup_existing)
    pending = {"package_id": keys.package_id, "relay": parse_key(key)[1], "delete_token": keys.delete_token,
               "source_host": manifest["source_host"], "time": time.time()}
    _pending_dir().mkdir(parents=True, exist_ok=True)
    pf = _pending_dir() / f"{keys.package_id}.json"
    paths.write_atomic(pf, json.dumps(pending))
    shutil.rmtree(d, ignore_errors=True)
    confirmed = _confirm(pending, pf)
    log.info("переезд принят: +%s, заменено %s, пропущено %s; сервер %s", len(out["added"]), len(out["replaced"]),
             len(out["skipped"]), "очищен" if confirmed else "ещё не подтверждён")
    return {**out, "warnings": res["warnings"], "confirmed": confirmed, "source_host": manifest["source_host"]}


def _confirm(pending: dict, pf: Path) -> bool:
    cl = Relay(pending["relay"], timeout=30)
    try:
        cl.finish(pending["package_id"], pending["delete_token"], "confirm")
    except MoveError as e:
        log.warning("подтверждение переезда не прошло: %s", e)
        return False
    finally:
        cl.close()
    pf.unlink(missing_ok=True)
    return True


def retry_pending_confirms() -> int:
    """При старте лаунчера: дослать подтверждения, которые не прошли из-за связи."""
    n = 0
    for pending in pending_imports():
        if _confirm(pending, _pending_dir() / f"{pending['package_id']}.json"):
            n += 1
    return n


def cancel_import(key: str) -> None:
    """Бросить начатый приём: удалить скачанное (пакет на сервере остаётся)."""
    secret, _ = parse_key(key)
    shutil.rmtree(_in_dir(derive(secret).package_id), ignore_errors=True)


# ---------------------------------------------------------------- фоновая задача для интерфейса


class Job:
    """Одна фоновая операция переезда (отправка, докачка, приём) с прогрессом для GET /api/move/status."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.state: dict = {"kind": None, "running": False}

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    def _progress(self, stage: str, done: int, total: int, **kw) -> None:
        with self._lock:
            self.state.update(stage=stage, done=done, total=total, **kw)

    def start(self, kind: str, fn: Callable[..., dict], *args, **kwargs) -> dict:
        with self._lock:
            if self.running:
                raise MoveError("переезд уже выполняется")
            self._cancel = threading.Event()
            self.state = {"kind": kind, "running": True, "stage": "start", "done": 0, "total": 0,
                          "error": None, "result": None, "started": time.time()}

        def run() -> None:
            try:
                res = fn(*args, progress=self._progress, cancel=self._cancel, **kwargs)
                upd = {"result": _public_result(res), "stage": "done"}
            except Cancelled:
                upd = {"stage": "cancelled"}
            except MoveError as e:
                upd = {"error": str(e), "stage": "error"}
            except Exception as e:  # noqa: BLE001
                log.exception("переезд")
                upd = {"error": f"{type(e).__name__}: {e}", "stage": "error"}
            with self._lock:
                self.state.update(upd, running=False, finished=time.time())

        self._thread = threading.Thread(target=run, name=f"move-{kind}", daemon=True)
        self._thread.start()
        return self.snapshot()

    def cancel(self, wait: float = 30) -> None:
        self._cancel.set()
        if self._thread:
            self._thread.join(wait)


def _public_result(res: dict) -> dict:
    """Без секрета: ключ отдаётся отдельно и только локально."""
    return {k: v for k, v in (res or {}).items() if k not in ("secret", "chunk_sha256", "key", "delete_token")}


job = Job()


def export_summary() -> dict | None:
    """Незавершённый переезд с этой машины (без секрета)."""
    st = load_state()
    if not st:
        return None
    return {"phase": st["phase"], "created": st["created"], "sent": st.get("sent"), "relay": st["relay"],
            "pull": {k: v for k, v in (st.get("pull") or {}).items() if k in ("to_name", "req")} or None,
            "profiles": st["profiles"], "cipher_bytes": st["cipher_bytes"], "key_id": key_id(st["key"]),
            "move_txt": str(move_txt())}


def relay_setting() -> str:
    return str((settings.load().get("move") or {}).get("relay") or "")


def set_relay(relay: str) -> dict:
    relay = normalize_relay(relay)
    cl = Relay(relay, timeout=10)
    try:
        h = cl.health()
    finally:
        cl.close()
    cur = settings.load().get("move") or {}
    settings.save({"move": {**cur, "relay": relay}})
    return {"relay": relay, "free_bytes": h.get("free_bytes")}


def check_relay(relay: str) -> dict:
    cl = Relay(relay, timeout=10)
    try:
        h = cl.health()
    finally:
        cl.close()
    return {"ok": True, "free_bytes": h.get("free_bytes")}
