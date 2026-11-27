"""Ярлыки и значок на панели задач (Windows).

Окно работает из папки версии (app/<версия>/python/XLauncher.exe), а старые версии
после обновления удаляются. Закреплённый на панели задач значок или ярлык, который
указывал на такую папку, после этого становится пустым. Поэтому:

  * окну задаём AppUserModelID и команду перезапуска — закрепляется стабильный
    xlauncher.exe из корня, а не exe версии;
  * держим ярлык XLauncher в меню «Пуск» с тем же AppUserModelID (Windows берёт
    из него иконку и имя группы на панели задач);
  * чиним уже существующие ярлыки (рабочий стол, «Пуск», закреплённые), которые
    смотрят внутрь app/.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from . import paths

log = logging.getLogger("xlauncher.winshell")

APP_ID = "susoev.XLauncher"
APP_NAME = "XLauncher"


def stable_exe() -> Path:
    return paths.root() / "xlauncher.exe"


def points_into_app(target: str, root: Path) -> bool:
    """Ярлык ведёт в папку версии (app/...) — после обновления он сломается."""
    if not target:
        return False
    t = os.path.normcase(os.path.normpath(target))
    app = os.path.normcase(os.path.normpath(str(root / "app")))
    return t.startswith(app + os.sep)


def shortcut_dirs() -> list[Path]:
    appdata = Path(os.environ.get("APPDATA", ""))
    home = Path(os.environ.get("USERPROFILE", ""))
    dirs = [
        appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        appdata / "Microsoft" / "Internet Explorer" / "Quick Launch" / "User Pinned" / "TaskBar",
        appdata / "Microsoft" / "Internet Explorer" / "Quick Launch" / "User Pinned" / "StartMenu",
        home / "Desktop",
        home / "OneDrive" / "Desktop",
    ]
    return [d for d in dirs if d.is_dir()]


# ---------------------------------------------------------------- Windows-only


class _com:
    """CoInitialize, терпимый к уже инициализированному потоку (STA/MTA)."""

    def __enter__(self):
        import pythoncom

        try:
            pythoncom.CoInitialize()
            self.ok = True
        except pythoncom.com_error:
            self.ok = False
        return self

    def __exit__(self, *exc):
        if self.ok:
            import pythoncom

            pythoncom.CoUninitialize()


def _pkey(name: str):
    from win32com.propsys import propsys

    return propsys.PSGetPropertyKeyFromName(name)


def _str(value: str):
    import pythoncom
    from win32com.propsys import propsys

    return propsys.PROPVARIANTType(value, pythoncom.VT_LPWSTR)


def _new_link():
    import pythoncom
    from win32com.shell import shell

    return pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                      shell.IID_IShellLink)


def _write_link(link, exe: Path, path: Path) -> None:
    import pythoncom
    from win32com.propsys import propsys

    link.SetPath(str(exe))
    link.SetArguments("")
    link.SetWorkingDirectory(str(exe.parent))
    link.SetIconLocation(str(exe), 0)
    link.SetDescription(APP_NAME)
    store = link.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(_pkey("System.AppUserModel.ID"), _str(APP_ID))
    store.Commit()
    link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), 0)


def _read_target(path: Path) -> tuple[object, str]:
    import pythoncom
    from win32com.shell import shell

    link = _new_link()
    link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(path), 0)
    return link, link.GetPath(shell.SLGP_RAWPATH)[0]


def ensure_shortcuts() -> None:
    """Ярлык в «Пуске» + починка ярлыков, ведущих в папку версии."""
    exe = stable_exe()
    if sys.platform != "win32" or not exe.exists():
        return
    with _com():
        root = paths.root()
        fixed = 0
        for d in shortcut_dirs():
            for lnk in d.glob("*.lnk"):
                try:
                    link, target = _read_target(lnk)
                    if points_into_app(target, root):
                        _write_link(link, exe, lnk)
                        fixed += 1
                        log.info("ярлык %s: %s -> %s", lnk, target, exe)
                except Exception as e:  # noqa: BLE001
                    log.debug("ярлык %s не прочитан: %s", lnk, e)
        start = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        own = start / f"{APP_NAME}.lnk"
        if start.is_dir():
            try:
                target = _read_target(own)[1] if own.exists() else ""
            except Exception:  # noqa: BLE001
                target = ""
            if os.path.normcase(target) != os.path.normcase(str(exe)):
                _write_link(_new_link(), exe, own)
                log.info("ярлык в «Пуске» создан: %s", own)
        if fixed:
            log.info("починено ярлыков: %d", fixed)


def _find_window() -> int:
    import win32gui
    import win32process

    found: list[int] = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == APP_NAME:
            if win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid():
                found.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    return found[0] if found else 0


def set_window_identity() -> None:
    """AppUserModelID и команда перезапуска окна: закрепляется xlauncher.exe из корня."""
    exe = stable_exe()
    if sys.platform != "win32" or not exe.exists():
        return
    from win32com.propsys import propsys

    with _com():
        hwnd = _find_window()
        if not hwnd:
            log.info("окно для значка не найдено")
            return
        store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
        store.SetValue(_pkey("System.AppUserModel.ID"), _str(APP_ID))
        store.SetValue(_pkey("System.AppUserModel.RelaunchCommand"), _str(f'"{exe}"'))
        store.SetValue(_pkey("System.AppUserModel.RelaunchDisplayNameResource"), _str(APP_NAME))
        store.SetValue(_pkey("System.AppUserModel.RelaunchIconResource"), _str(f"{exe},0"))
        try:  # хранилище окна применяет значения сразу, Commit может быть не реализован
            store.Commit()
        except Exception:  # noqa: BLE001
            pass
        log.info("окну задан значок и команда перезапуска (%s)", exe)


def safe(fn) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001
        log.exception("%s не удалось", fn.__name__)
