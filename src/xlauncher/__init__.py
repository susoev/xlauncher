"""xlauncher — лаунчер изолированных профилей Chromium."""

from pathlib import Path

_vf = Path(__file__).with_name("VERSION")
__version__ = _vf.read_text(encoding="utf-8").strip() if _vf.exists() else "0.0.0"


def is_dev() -> bool:
    """Версия без файла VERSION — запуск из исходников, не из релиза."""
    return __version__ == "0.0.0"
