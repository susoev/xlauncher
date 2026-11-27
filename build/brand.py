"""Иконка и описание exe (то, что показывает диспетчер задач и проводник).

    python build/brand.py <exe> "<описание>" <версия>

Использует resedit-cli (npm, кроссплатформенный). Подпись исходного exe при этом снимается,
поэтому после ресурсов exe подписывается своим сертификатом (build/sign.py), если задан ключ.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ICON = ROOT / "src" / "xlauncher" / "assets" / "xlauncher.ico"
RESEDIT = ["npx", "--yes", "resedit-cli@3"]


def brand(exe: Path, description: str, version: str) -> None:
    if not shutil.which("npx"):
        raise SystemExit("нужен Node.js (npx) для resedit-cli")
    file_version = ".".join((version.split(".") + ["0"] * 4)[:4])
    subprocess.run([*RESEDIT, "--in", str(exe), "--out", str(exe), "--ignore-signed",
                    "--delete-group-icon", "--delete-icon", "--icon", f"1,{ICON}",
                    "--file-description", description, "--product-name", "XLauncher", "--company-name", "susoev",
                    "--file-version", file_version, "--product-version", file_version,
                    "--original-filename", exe.name, "--internal-name", exe.stem], check=True)
    from sign import sign

    sign(exe)


if __name__ == "__main__":
    brand(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
