"""Локальный HTTP-сервер: API и страница интерфейса. Слушает только 127.0.0.1."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from . import __version__, paths

if TYPE_CHECKING:
    from .updater import Updater


def create_app(updater: "Updater | None") -> FastAPI:
    app = FastAPI(title="xlauncher", version=__version__, docs_url=None, redoc_url=None)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": __version__}

    @app.get("/api/status")
    def status() -> dict:
        return {
            "version": __version__,
            "pid": os.getpid(),
            "root": str(paths.root()),
            "updater": updater.status() if updater else {"state": "выключено (запуск из исходников)"},
        }

    @app.post("/api/update/check")
    def update_check() -> dict:
        if updater:
            updater.check_now()
        return {"ok": bool(updater)}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML

    return app


INDEX_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>xlauncher</title>
<style>
  :root { color-scheme: light dark; --fg:#1d1d1f; --mut:#6e6e73; --bg:#fbfbfd; --card:#fff; --line:#e5e5ea; --acc:#0a66ff; }
  @media (prefers-color-scheme: dark) { :root { --fg:#f2f2f7; --mut:#98989f; --bg:#111113; --card:#1c1c1f; --line:#2c2c30; } }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 "Segoe UI", system-ui, sans-serif; color:var(--fg); background:var(--bg); }
  main { max-width:720px; margin:0 auto; padding:32px 24px; }
  h1 { font-size:22px; margin:0 0 4px; }
  .mut { color:var(--mut); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 20px; margin-top:20px; }
  dl { display:grid; grid-template-columns:180px 1fr; gap:6px 12px; margin:0; }
  dt { color:var(--mut); } dd { margin:0; font-variant-numeric:tabular-nums; word-break:break-all; }
  button { font:inherit; padding:8px 14px; border-radius:8px; border:1px solid var(--line); background:var(--card); color:var(--fg); cursor:pointer; }
  button:hover { border-color:var(--acc); }
  .err { color:#d70015; }
</style></head>
<body><main>
  <h1>xlauncher <span id="ver" class="mut"></span></h1>
  <div class="mut">Эта версия приехала автообновлением. Профили появятся на следующем этапе.</div>
  <div class="card"><dl id="st"></dl></div>
  <div class="card"><button id="chk">Проверить обновления сейчас</button> <span id="msg" class="mut"></span></div>
</main>
<script>
const labels = {state:'Состояние', last_check:'Последняя проверка', latest_seen:'Последний релиз',
  installed:'Установлено', bootstrap_version:'Версия загрузчика', bad_versions:'Сбойные версии', last_error:'Ошибка'};
async function load() {
  try {
    const s = await (await fetch('/api/status')).json();
    document.getElementById('ver').textContent = s.version;
    const u = s.updater || {};
    const rows = [['Папка', s.root], ...Object.entries(labels).filter(([k]) => u[k] !== undefined && u[k] !== null && !(Array.isArray(u[k]) && !u[k].length)).map(([k, t]) => [t, Array.isArray(u[k]) ? u[k].join(', ') : u[k]])];
    document.getElementById('st').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd${k === 'Ошибка' ? ' class="err"' : ''}>${String(v).replace(/</g, '&lt;')}</dd>`).join('');
  } catch (e) {}
}
document.getElementById('chk').onclick = async () => {
  await fetch('/api/update/check', {method: 'POST'});
  document.getElementById('msg').textContent = 'Проверяю…';
  setTimeout(() => document.getElementById('msg').textContent = '', 4000);
};
load(); setInterval(load, 3000);
</script></body></html>
"""
