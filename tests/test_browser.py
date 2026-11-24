"""Управление браузером на настоящем Chrome (пропускается, если Chrome нет)."""

import asyncio
import functools
import shutil
import socket
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from xlauncher.core import browser as b

CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
pytestmark = pytest.mark.skipif(not CHROME, reason="нет Chrome")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Форма</title></head><body>
<label for="login">Логин</label><input id="login">
<label><input type="checkbox"> Запомнить</label>
<button onclick="document.getElementById('out').textContent='Привет, ' + document.getElementById('login').value + (event.isTrusted ? ' (trusted)' : '')">Войти</button>
<div id="out"></div><a href="two.html">Дальше</a>
<script>window.__mut = 0; new MutationObserver(m => window.__mut += m.length).observe(document, {attributes: true, subtree: true});</script>
</body></html>"""


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def chrome(tmp_path_factory):
    site = tmp_path_factory.mktemp("site")
    (site / "index.html").write_text(PAGE, encoding="utf-8")
    (site / "two.html").write_text('<meta charset="utf-8"><title>Два</title><p>Вторая страница', encoding="utf-8")
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(site))
    handler.log_message = lambda *a: None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cdp = _port()
    proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--no-first-run",
                             f"--user-data-dir={tmp_path_factory.mktemp('ud')}", f"--remote-debugging-port={cdp}",
                             f"http://127.0.0.1:{srv.server_address[1]}/"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            if b.tabs(cdp):
                break
        except Exception:
            time.sleep(0.2)
    time.sleep(1)
    yield cdp
    proc.terminate()
    proc.wait(10)
    srv.shutdown()


def test_snapshot_type_click_without_dom_traces(chrome):
    async def go():
        snap = await b.snapshot(chrome)
        ref = {e["name"]: e["ref"] for e in snap["elements"]}
        assert {"Логин", "Запомнить", "Войти", "Дальше"} <= set(ref)
        await b.type_text(chrome, ref["Логин"], "Клод")
        await b.click(chrome, ref["Запомнить"])
        await b.click(chrome, ref["Войти"])
        assert (await b.wait_for(chrome, "Привет, Клод (trusted)", 5))["found"]
        after = await b.snapshot(chrome)
        box = next(e for e in after["elements"] if e["name"] == "Запомнить")
        assert "отмечен" in box["extra"]
        async with b.Session(chrome) as s:  # основной мир страницы: никаких изменений атрибутов
            r = await s.send("Runtime.evaluate", {"expression": "window.__mut", "returnByValue": True})
        assert r["result"]["value"] == 0
        assert len(await b.screenshot(chrome)) > 1000
        await b.click(chrome, ref["Дальше"])
        assert (await b.wait_for(chrome, "Вторая страница", 5))["found"]
        with pytest.raises(b.BrowserError, match="снимок заново"):
            await b.click(chrome, "e99999999")

    asyncio.run(go())
