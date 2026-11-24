"""xlauncher — лаунчер изолированных профилей Chromium."""

import site
import sys
from pathlib import Path

# Сборка со встроенным Python: зависимости лежат в ../lib рядом с src. Регистрируем её как
# site-папку, чтобы обработались .pth-файлы (pywin32 добавляет свои пути и DLL через них).
_lib = Path(__file__).resolve().parents[2] / "lib"
if sys.platform == "win32" and _lib.is_dir() and str(_lib) in sys.path:
    site.addsitedir(str(_lib))

_vf = Path(__file__).with_name("VERSION")
__version__ = _vf.read_text(encoding="utf-8").strip() if _vf.exists() else "0.0.0"


def is_dev() -> bool:
    """Версия без файла VERSION — запуск из исходников, не из релиза."""
    return __version__ == "0.0.0"
