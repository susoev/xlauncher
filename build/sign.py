"""Подпись exe собственным сертификатом (Authenticode, osslsigncode).

    python build/sign.py <exe>...

Ключ — из переменных XL_SIGN_PFX (pfx в base64) и XL_SIGN_PASS (секреты GitHub Actions).
Нет переменных — подпись пропускается. Открытая часть сертификата — build/codesign.cer:
её ставят в доверенные на своих ПК (README, «Подпись»).
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TIMESTAMP = "http://timestamp.digicert.com"


def enabled() -> bool:
    return bool(os.environ.get("XL_SIGN_PFX"))


def sign(exe: Path) -> None:
    if not enabled():
        return
    if not shutil.which("osslsigncode"):
        raise SystemExit("нужен osslsigncode для подписи (XL_SIGN_PFX задан)")
    with tempfile.TemporaryDirectory() as tmp:
        pfx = Path(tmp) / "k.pfx"
        pfx.write_bytes(base64.b64decode(os.environ["XL_SIGN_PFX"]))
        pfx.chmod(0o600)
        pw = Path(tmp) / "p.txt"
        pw.write_text(os.environ.get("XL_SIGN_PASS", ""), encoding="utf-8")
        out = Path(tmp) / exe.name
        base = ["osslsigncode", "sign", "-pkcs12", str(pfx), "-readpass", str(pw), "-h", "sha256",
                "-n", "XLauncher", "-in", str(exe), "-out", str(out)]
        # метка времени — подпись останется действительной после истечения сертификата;
        # сервер недоступен — подписываем без неё
        if subprocess.run([*base, "-ts", TIMESTAMP], capture_output=True).returncode != 0:
            out.unlink(missing_ok=True)
            subprocess.run(base, check=True, capture_output=True)
        shutil.move(out, exe)
    print(f"подписан {exe.name}")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        sign(Path(p))
