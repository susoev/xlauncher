"""Выход приложения с нужным кодом для загрузчика (75 — перезапуск, 76 — очистка после переезда)."""

from __future__ import annotations

from typing import Callable

EXIT_WIPE = 76

exit_code: int | None = None
_on_exit: Callable[[], None] | None = None


def register(callback: Callable[[], None]) -> None:
    global _on_exit
    _on_exit = callback


def request_exit(code: int) -> None:
    global exit_code
    exit_code = code
    if _on_exit:
        _on_exit()


def bootstrap_version() -> int:
    import os

    try:
        return int(os.environ.get("XL_BOOTSTRAP_VERSION") or 0)
    except ValueError:
        return 0
