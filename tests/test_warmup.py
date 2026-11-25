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


def test_day_routine_plan(xl_root):
    sites = [{"name": f"С{i}", "domain": f"s{i}.ru", "url": f"https://s{i}.ru/", "dwell_sec": [40, 60],
              "yandex": i == 0, "enabled": i != 1} for i in range(8)]
    (xl_root / "warmup").mkdir()
    (xl_root / "warmup" / "sites.json").write_text(json.dumps({"sites": sites, "day_routine": {"sites": [3, 4]}}),
                                                  encoding="utf-8")
    for _ in range(20):
        picked, engine, timing = warmup.day_routine_plan()
        assert 3 <= len(picked) <= 4
        assert engine == "yandex"
        assert "s1.ru" not in {s["domain"] for s in picked}  # выключенный — мимо
        assert all(s["dwell_sec"] == warmup.DAY_ROUTINE["dwell_sec"] for s in picked)
        assert timing["keep_tab_chance"] == 0 and timing["final_round"] is False


def test_day_routine_via_yandex(tmp_path, monkeypatch, xl_root):
    site_hits, search_hits = [], []
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text('<meta charset="utf-8"><title>Сайт</title><h1>Главная</h1>'
                                     '<div style="height:3000px"></div>', encoding="utf-8")
    site_srv = _serve(site, site_hits)
    sport = site_srv.server_address[1]
    search = tmp_path / "search"
    search.mkdir()
    (search / "home.html").write_text(
        '<meta charset="utf-8"><input name="text" style="width:300px" onkeydown="if (event.key === \'Enter\') '
        'location = \'/search.html?text=\' + encodeURIComponent(this.value)">', encoding="utf-8")
    (search / "search.html").write_text(
        f'<meta charset="utf-8"><a href="http://localhost:{sport}/index.html"><h3>Тестовый сайт localhost</h3></a>',
        encoding="utf-8")
    search_srv = _serve(search, search_hits)
    monkeypatch.setitem(warmup.SEARCH, "yandex", {"home": f"http://127.0.0.1:{search_srv.server_address[1]}/home.html",
                                                  "input": "input[name=text]"})
    sites = [{"name": f"Тест {i}", "domain": "localhost", "url": f"http://localhost:{sport}/index.html",
              "query": f"тест {i}", "enabled": True} for i in range(5)]
    (xl_root / "warmup").mkdir()
    (xl_root / "warmup" / "sites.json").write_text(json.dumps({
        "sites": sites, "day_routine": {"sites": [2, 2], "dwell_sec": [6, 8], "between_sites_sec": [1, 2]}}),
        encoding="utf-8")

    async def fast_pause(self, a, c):
        if self.stop.is_set():
            raise warmup.Stopped()
        await asyncio.sleep(min(a, c) / 4)

    monkeypatch.setattr(warmup.Runner, "pause", fast_pause)
    cdp = _port()
    proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--no-first-run",
                             f"--user-data-dir={tmp_path / 'ud'}", f"--remote-debugging-port={cdp}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    journal = []
    try:
        for _ in range(100):
            try:
                if b.tabs(cdp):
                    break
            except Exception:
                time.sleep(0.2)
        picked, engine, timing = warmup.day_routine_plan()
        r = warmup.Runner(cdp, lambda st: None, asyncio.Event(), journal.append, label="day_routine", timing=timing)
        res = asyncio.run(r.run(picked, engine))
    finally:
        proc.terminate()
        proc.wait(10)
        site_srv.shutdown()
        search_srv.shutdown()

    assert res["status"] == "done", res
    assert len(res["results"]) == 2
    for one in res["results"]:
        assert one["result"] == "ok" and one["via"] == "yandex", one
    assert sum(h.startswith("/search.html?text=") for h in search_hits) == 2
    assert res["kept_tabs"] == [] and not res.get("revisits")  # без «возврата посетителя»
    assert all(line.startswith("day_routine: localhost\tok\tyandex") for line in journal), journal


def test_day_routine_weights(xl_root, monkeypatch):
    sites = [{"name": "Соцсеть", "domain": "social.ru", "url": "https://social.ru/", "weight": 10},
             {"name": "Налоговая", "domain": "tax.ru", "url": "https://tax.ru/", "weight": 1},
             {"name": "Яндекс", "domain": "ya-site.ru", "url": "https://ya-site.ru/", "weight": 5, "yandex": True}]
    (xl_root / "warmup").mkdir()
    f = xl_root / "warmup" / "sites.json"
    f.write_text(json.dumps({"sites": sites, "day_routine": {"sites": [1, 1], "include_yandex": False}}), encoding="utf-8")
    hits = {"social.ru": 0, "tax.ru": 0}
    for _ in range(2000):
        hits[warmup.day_routine_plan()[0][0]["domain"]] += 1
    assert hits["social.ru"] > 6 * hits["tax.ru"], hits  # ожидаемо 10:1
    f.write_text(json.dumps({"sites": sites, "day_routine": {"sites": [3, 3]}}), encoding="utf-8")
    assert {s["domain"] for s in warmup.day_routine_plan()[0]} == {"social.ru", "tax.ru", "ya-site.ru"}


def test_builtin_sites_have_weights():
    d = json.loads(warmup.BUILTIN.read_text(encoding="utf-8"))
    domains = {s["domain"] for s in d["sites"]}
    assert {"dzen.ru", "tenchat.ru", "vk.ru", "vk.com"} <= domains
    w = {s["domain"]: s["weight"] for s in d["sites"]}
    assert all(isinstance(v, int) and 1 <= v <= 10 for v in w.values()), w
    assert min(w["vk.com"], w["tenchat.ru"], w["dzen.ru"]) > w["rbc.ru"] > w["nalog.gov.ru"]
