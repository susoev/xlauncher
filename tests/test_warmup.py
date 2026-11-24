"""Сценарий прогрева на настоящем Chrome с локальными «поисковиком» и «сайтом»."""

import asyncio
import functools
import json
import shutil
import socket
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from xlauncher.core import browser as b
from xlauncher.core import warmup

CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
pytestmark = pytest.mark.skipif(not CHROME, reason="нет Chrome")


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(directory, hits):
    class H(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            return super().do_GET()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(directory)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_warmup_via_search_skips_ads(tmp_path, monkeypatch, xl_root):
    site_hits, search_hits = [], []
    site = tmp_path / "site"
    site.mkdir()
    links = "".join(f'<p><a href="/a{i}.html">Интересная статья номер {i} про всё</a></p>' for i in range(5))
    (site / "index.html").write_text(f'<meta charset="utf-8"><title>Сайт</title><h1>Главная</h1>{links}'
                                     '<div style="height:3000px"></div>', encoding="utf-8")
    for i in range(5):
        (site / f"a{i}.html").write_text(f'<meta charset="utf-8"><title>Статья {i}</title><p>Текст</p>'
                                         '<div style="height:3000px"></div>', encoding="utf-8")
    (site / "ad.html").write_text("реклама", encoding="utf-8")
    site_srv = _serve(site, site_hits)
    sport = site_srv.server_address[1]

    search = tmp_path / "search"
    search.mkdir()
    (search / "home.html").write_text(
        '<meta charset="utf-8"><input name="q" style="width:300px" onkeydown="if (event.key === \'Enter\') '
        'location = \'/search-results.html?q=\' + encodeURIComponent(this.value)">', encoding="utf-8")
    (search / "search-results.html").write_text(
        f'<meta charset="utf-8"><div id="tads"><a href="http://localhost:{sport}/ad.html">Реклама localhost — лучший сайт</a></div>'
        f'<div><a href="http://localhost:{sport}/index.html"><span>http://localhost</span><br><br><h3 style="display:inline">'
        f'Тестовый сайт — официальный</h3></a></div>', encoding="utf-8")
    search_srv = _serve(search, search_hits)
    monkeypatch.setitem(warmup.SEARCH, "google", {"home": f"http://127.0.0.1:{search_srv.server_address[1]}/home.html",
                                                  "input": "input[name=q]"})
    sites = [{"name": f"Тест {i}", "domain": "localhost", "url": f"http://localhost:{sport}/index.html",
              "query": f"тест сайт {i}", "dwell_sec": [14, 16], "enabled": True} for i in (1, 2)]
    timing = {"keep_tabs_max": 2, "keep_tab_chance": 1, "revisit_chance": 1, "revisit_in_browse_chance": 0,
              "revisit_sec": [6, 8], "revisit_click_chance": 0, "internal_pages": [1, 1], "between_sites_sec": [1, 2]}
    (xl_root / "warmup").mkdir()
    (xl_root / "warmup" / "sites.json").write_text(json.dumps({"sites": sites, "timing": timing}), encoding="utf-8")

    async def fast_pause(self, a, c):
        if self.stop.is_set():
            raise warmup.Stopped()
        await asyncio.sleep(min(a, c) / 4)

    monkeypatch.setattr(warmup.Runner, "pause", fast_pause)

    cdp = _port()
    proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--no-first-run",
                             f"--user-data-dir={tmp_path / 'ud'}", f"--remote-debugging-port={cdp}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                if b.tabs(cdp):
                    break
            except Exception:
                time.sleep(0.2)
        progress = []
        r = warmup.Runner(cdp, progress.append, asyncio.Event())
        res = asyncio.run(r.run(warmup.pick_sites(5, False), "google"))
    finally:
        proc.terminate()
        proc.wait(10)
        site_srv.shutdown()
        search_srv.shutdown()

    assert res["status"] == "done", res
    for one in res["results"]:
        assert one["result"] == "ok" and one["via"] == "google", one
        assert one["pages"] >= 2, one  # главная + хотя бы одна внутренняя
    assert "/ad.html" not in site_hits  # рекламу не трогали
    assert sum(h.startswith("/search-results.html?q=") for h in search_hits) == 2
    assert progress[-1]["done"] == 2
    # «возврат посетителя»: оба сайта остались во вкладках, к ним возвращались (между сайтами и в финальном круге)
    assert res["kept_tabs"] == ["localhost", "localhost"]
    assert res.get("revisits", 0) >= 3
