"""Сборка пакета приложения под Windows x64 (запускается на Linux в CI).

    python build/build_app.py --version 2026.11.24.7 --out dist [--python-zip путь]

Результат в dist/:
  xlauncher-app-<версия>.zip — встроенный Python + зависимости + код
  manifest.json              — версия, имена файлов, SHA-256 (+ загрузчик, если dist/xlauncher.exe уже собран)

Структура архива:
  python/   — Python embeddable (pythonw.exe), ._pth указывает на ../lib и ../src
  lib/      — зависимости (pip --platform win_amd64)
  src/xlauncher/ — код + VERSION
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
PY_MINOR = "3.13"
# Чистый Python, выпускаются только исходниками: собираем wheel локально,
# иначе pip с --only-binary откатывается на древние версии зависящих пакетов.
SDIST_ONLY = ["proxy_tools"]
FTP = "https://www.python.org/ftp/python/"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def latest_embeddable(minor: str) -> tuple[str, bytes]:
    """Последний патч-релиз minor, для которого выложен embeddable zip."""
    index = urllib.request.urlopen(FTP, timeout=60).read().decode()
    versions = sorted({v for v in re.findall(rf'href="({re.escape(minor)}\.\d+)/"', index)},
                      key=lambda v: int(v.rsplit(".", 1)[1]), reverse=True)
    for v in versions:
        url = f"{FTP}{v}/python-{v}-embed-amd64.zip"
        try:
            data = urllib.request.urlopen(url, timeout=120).read()
            print(f"Python embeddable {v}")
            return v, data
        except Exception:  # noqa: BLE001
            continue
    raise SystemExit(f"Не найден embeddable zip для Python {minor}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", default="dist")
    ap.add_argument("--python-zip", help="локальный python-3.13.x-embed-amd64.zip (иначе скачать)")
    ap.add_argument("--no-brand", action="store_true", help="не менять иконку и описание exe (без Node.js)")
    a = ap.parse_args()

    out = (ROOT / a.out).resolve()
    stage = out / "stage"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)

    # 1. Python embeddable
    if a.python_zip:
        data = Path(a.python_zip).read_bytes()
    else:
        _, data = latest_embeddable(PY_MINOR)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(stage / "python")
    pth = next((stage / "python").glob("python3*._pth"))
    stdlib_zip = next((stage / "python").glob("python3*.zip")).name
    pth.write_text(f"{stdlib_zip}\n.\n..\\lib\n..\\src\nimport site\n", encoding="ascii")
    # Свои имена процессов вместо «Python» в диспетчере задач. ._pth назван по DLL (python3XX._pth),
    # поэтому встроенный Python находит его при любом имени exe.
    py = stage / "python"
    for name, desc in (("XLauncher.exe", "XLauncher"), ("xlauncher-runner.exe", "XLauncher — профиль")):
        shutil.copy2(py / "pythonw.exe", py / name)
        if not a.no_brand:
            from brand import brand

            brand(py / name, desc, a.version)

    # 2. Зависимости под Windows x64
    wheelhouse = out / "wheelhouse"
    shutil.rmtree(wheelhouse, ignore_errors=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--quiet", "--no-deps", "-w", str(wheelhouse), *SDIST_ONLY],
                   check=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--no-compile",
         "--target", str(stage / "lib"),
         "--platform", "win_amd64", "--python-version", PY_MINOR, "--implementation", "cp",
         "--only-binary=:all:", "--find-links", str(wheelhouse), "-r", str(ROOT / "requirements.txt")],
        check=True,
    )
    for junk in (stage / "lib").glob("**/__pycache__"):
        shutil.rmtree(junk, ignore_errors=True)
    shutil.rmtree(stage / "lib" / "bin", ignore_errors=True)

    # 3. Код
    shutil.copytree(ROOT / "src" / "xlauncher", stage / "src" / "xlauncher",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (stage / "src" / "xlauncher" / "VERSION").write_text(a.version, encoding="utf-8")

    # 4. Архив
    asset = f"xlauncher-app-{a.version}.zip"
    zpath = out / asset
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(stage.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(stage).as_posix())
    shutil.rmtree(stage)
    shutil.rmtree(wheelhouse, ignore_errors=True)

    # 5. Манифест
    manifest = {"version": a.version, "app_asset": asset, "app_sha256": sha256(zpath)}
    m = re.search(r"const bootstrapVersion = (\d+)", (ROOT / "bootstrap" / "common.go").read_text())
    exe = out / "xlauncher.exe"
    if m and exe.exists():
        manifest |= {"bootstrap_version": int(m.group(1)), "bootstrap_asset": "xlauncher.exe",
                     "bootstrap_sha256": sha256(exe)}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"{asset}: {zpath.stat().st_size / 1e6:.1f} МБ")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
