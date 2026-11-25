"""Запуск и обслуживание релея.

  python -m xl_relay serve        слушать XL_RELAY_BIND:XL_RELAY_PORT (ждёт, пока адрес появится)
  python -m xl_relay list         пакеты на сервере: id, источник, дата, размер, статус
  python -m xl_relay rm <id>      удалить пакет вручную (спросит подтверждение)
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import time

from .app import _store, create_app, list_packages, remove_package


def _wait_bind(host: str, port: int) -> None:
    """Tailscale-адрес появляется не сразу после загрузки — ждём его, а не падаем."""
    while True:
        try:
            with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
            return
        except OSError as e:
            logging.warning("адрес %s:%s пока недоступен (%s), жду", host, port, e)
            time.sleep(5)


def serve() -> int:
    import uvicorn

    host = os.environ.get("XL_RELAY_BIND", "").strip()
    port = int(os.environ.get("XL_RELAY_PORT", "47120"))
    if not host or host in ("0.0.0.0", "::"):
        print("XL_RELAY_BIND: нужен конкретный адрес (Tailscale-адрес машины), не 0.0.0.0", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _store().mkdir(parents=True, exist_ok=True)
    _wait_bind(host, port)
    uvicorn.run(create_app(), host=host, port=port, log_level="warning", access_log=False)
    return 0


def _size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "serve"
    if cmd == "serve":
        return serve()
    if cmd == "list":
        rows = list_packages()
        for r in rows:
            print(f"{r['id'][:8]}  {r['source'] or '?':<16} {r['created'] or '':<26} {r['profiles'] or 0:>3} проф.  "
                  f"{_size(r['bytes']):>9}  {r['status']}")
        if not rows:
            print("пакетов нет")
        return 0
    if cmd == "rm" and len(argv) == 2:
        full = [r["id"] for r in list_packages() if r["id"].startswith(argv[1])]
        if len(full) != 1:
            print("нет такого пакета или префикс неоднозначен", file=sys.stderr)
            return 1
        if input(f"Удалить пакет {full[0][:8]} без возможности восстановления? [y/N] ").strip().lower() != "y":
            return 1
        print("удалён" if remove_package(full[0]) else "не найден")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
