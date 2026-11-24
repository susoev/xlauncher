"""Точка входа приложения: python -m xlauncher [--selftest].

Запускается загрузчиком (xlauncher.exe). Коды выхода:
  0  — пользователь закрыл окно, загрузчик тоже завершается;
  75 — установлено обновление, загрузчик перезапускает новую версию.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from . import __version__, is_dev, paths

EXIT_RESTART = 75
DEFAULT_PORT = 47110

log = logging.getLogger("xlauncher")


def setup_logging() -> None:
    paths.ensure()
    h = logging.handlers.RotatingFileHandler(paths.logs_dir() / "app.log", maxBytes=2_000_000, backupCount=3,
                                             encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[h])
    if sys.stderr is not None:  # под pythonw stderr отсутствует
        logging.getLogger().addHandler(logging.StreamHandler())


def pick_port(preferred: int = DEFAULT_PORT) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("нет свободного порта")


def start_server(app, port: int):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None))
    t = threading.Thread(target=server.run, name="server", daemon=True)
    t.start()
    for _ in range(200):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("сервер не запустился")


def selftest() -> int:
    """Проверка сборки: импорты, сервер, окно (без показа). Используется в CI."""
    from .server import create_app

    port = pick_port(0)
    start_server(create_app(None), port)
    body = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5).read())
    assert body["ok"] and body["version"] == __version__, body
    if sys.platform == "win32":
        import webview  # noqa: F401
        from webview.platforms import winforms  # noqa: F401  — проверяет pythonnet/.NET
    print(f"selftest OK {__version__}")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    setup_logging()
    log.info("старт %s, pid %s, корень %s", __version__, os.getpid(), paths.root())

    from .server import create_app
    from .updater import Updater

    restart = threading.Event()
    window_ref: dict = {}

    def on_update_ready(v: str) -> None:
        log.info("обновление %s установлено — перезапуск", v)
        restart.set()
        w = window_ref.get("w")
        if w is not None:
            try:
                w.destroy()
            except Exception:  # noqa: BLE001
                pass

    updater = None if is_dev() else Updater(__version__, on_update_ready)

    port = pick_port()
    start_server(create_app(updater), port)
    url = f"http://127.0.0.1:{port}/"
    paths.write_atomic(paths.run_dir() / "app.json",
                       json.dumps({"version": __version__, "pid": os.getpid(), "port": port}))
    # Сигнал загрузчику: версия поднялась и работает.
    (paths.run_dir() / f"ready-{__version__}").write_text(str(os.getpid()), encoding="utf-8")
    log.info("готово: %s", url)

    if updater:
        updater.cleanup()
        updater.start()

    def watch_show_requests() -> None:
        f = paths.run_dir() / "show-request"
        while not restart.is_set():
            if f.exists():
                f.unlink(missing_ok=True)
                w = window_ref.get("w")
                try:
                    if w is not None:
                        w.restore()
                        w.show()
                    else:
                        webbrowser.open(url)
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(1)

    threading.Thread(target=watch_show_requests, name="show", daemon=True).start()

    try:
        import webview

        window_ref["w"] = webview.create_window(f"xlauncher {__version__}", url, width=880, height=640,
                                                min_size=(640, 480))
        webview.start()
    except Exception:  # noqa: BLE001
        log.exception("окно не открылось — открываю интерфейс в браузере по умолчанию")
        webbrowser.open(url)
        restart.wait()

    return EXIT_RESTART if restart.is_set() else 0


if __name__ == "__main__":
    sys.exit(main())
