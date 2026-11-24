"""Перенос профилей между компьютерами: ключ шифрования кук и lock-файлы (ARCHITECTURE.md §7).

Chromium на Windows шифрует куки AES-ключом из data/Local State, а сам ключ — DPAPI
(привязан к пользователю Windows). Сохраняем «сырой» ключ в os_crypt.key и на другом
компьютере перешифровываем его под текущего пользователя. Проверено на движке 148 (spike/).
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("xlauncher.portability")

KEY_FILE = "os_crypt.key"
LOCK_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile")


# ---------------------------------------------------------------- DPAPI


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI доступен только на Windows")
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    inp = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = DATA_BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError(f"DPAPI {'protect' if protect else 'unprotect'}: ошибка {ctypes.GetLastError()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def dpapi_protect(data: bytes) -> bytes:
    return _dpapi(data, True)


def dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi(data, False)


# ---------------------------------------------------------------- ключ


def _local_state(data_dir: Path) -> Path:
    return data_dir / "Local State"


def _read_encrypted_key(data_dir: Path) -> bytes | None:
    try:
        st = json.loads(_local_state(data_dir).read_text(encoding="utf-8"))
        enc = base64.b64decode(st["os_crypt"]["encrypted_key"])
    except (OSError, ValueError, KeyError):
        return None
    return enc[5:] if enc.startswith(b"DPAPI") else None


def current_key(data_dir: Path) -> bytes | None:
    """Ключ из Local State, если он расшифровывается на этом компьютере."""
    blob = _read_encrypted_key(data_dir)
    if blob is None:
        return None
    try:
        return dpapi_unprotect(blob)
    except OSError:
        return None


def save_key(profile_dir: Path) -> bool:
    """Сохраняет ключ в os_crypt.key, если его ещё нет. True — ключ сохранён сейчас."""
    kf = profile_dir / KEY_FILE
    if kf.exists():
        return False
    key = current_key(profile_dir / "data")
    if not key:
        return False
    kf.write_bytes(key)
    log.info("ключ шифрования сохранён: %s", profile_dir.name)
    return True


def restore_key(profile_dir: Path) -> str:
    """Готовит Local State к запуску на этом компьютере.

    new        — профиль ещё не запускался (Local State нет)
    ok         — ключ расшифровывается, ничего делать не нужно
    restored   — профиль перенесён, ключ перешифрован под этого пользователя
    no-backup  — ключ не расшифровывается, а os_crypt.key нет: куки будут потеряны
    """
    data_dir = profile_dir / "data"
    if _read_encrypted_key(data_dir) is None:
        return "new"
    if current_key(data_dir) is not None:
        return "ok"
    kf = profile_dir / KEY_FILE
    if not kf.exists():
        return "no-backup"
    p = _local_state(data_dir)
    st = json.loads(p.read_text(encoding="utf-8"))
    st["os_crypt"]["encrypted_key"] = base64.b64encode(b"DPAPI" + dpapi_protect(kf.read_bytes())).decode()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    os.replace(tmp, p)
    log.info("ключ шифрования восстановлен после переноса: %s", profile_dir.name)
    return "restored"


def clear_locks(data_dir: Path) -> None:
    for n in LOCK_FILES:
        try:
            (data_dir / n).unlink(missing_ok=True)
        except OSError:
            pass
