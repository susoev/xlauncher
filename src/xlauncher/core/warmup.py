"""Прогрев профиля: обход площадок через поиск, как это делал бы человек.

Без нейросети: для каждого сайта из warmup/sites.json —
  поиск (Google или Яндекс) → клик по органической ссылке на домен (реклама пропускается) →
  случайное время на сайте из dwell_sec: листание с паузами, 1–2 внутренние ссылки → следующий сайт.
Капча не проходится: сайт помечается и пропускается.

Выполняется в раннере профиля (переживает перезапуск окна лаунчера при обновлении).
Требует запуска профиля с доступом агента (порт отладки).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.parse
from pathlib import Path
from typing import Callable

from .. import paths
from . import browser
from .browser import Session

log = logging.getLogger("xlauncher.warmup")

BUILTIN = Path(__file__).resolve().parents[1] / "warmup" / "sites.json"

SEARCH = {
    "google": {"home": "https://www.google.com/", "input": "textarea[name=q], input[name=q]"},
    "yandex": {"home": "https://ya.ru/", "input": "input[name=text], textarea[name=text]"},
}

CAPTCHA_MARKERS = ("showcaptcha", "/sorry/", "captcha", "smartcaptcha")

# Тайминги по умолчанию; переопределяются блоком "timing" в sites.json.
TIMING = {
    "between_sites_sec": [5, 20],        # пауза между сайтами
    "page_load_timeout_sec": 30,         # сколько ждать загрузки страницы
    "after_load_sec": [1, 3],            # пауза после загрузки, прежде чем что-то делать
    "scroll_pause_sec": [1.5, 6],        # пауза между сериями прокрутки
    "scroll_ticks": [2, 6],              # «щелчков» колеса в одной серии
    "scroll_tick_px": [80, 140],         # пикселей за щелчок
    "scroll_up_chance": 0.12,            # вероятность прокрутить вверх
    "reading_pause_chance": 0.2,         # вероятность длинной паузы «на чтение»
    "reading_pause_sec": [8, 15],
    "internal_pages": [1, 2],            # сколько внутренних страниц открыть на сайте
    "typing_delay_sec": [0.05, 0.22],    # задержка между символами в поиске
    # «возврат посетителя»: часть сайтов остаётся во вкладках, к ним периодически возвращаемся
    "keep_tabs_max": 3,                  # сколько вкладок с сайтами держать открытыми
    "keep_tab_chance": 0.5,              # вероятность оставить сайт во вкладке
    "revisit_chance": 0.4,               # вероятность вернуться к открытой вкладке между сайтами
    "revisit_in_browse_chance": 0.08,    # то же — во время чтения другого сайта (за серию прокрутки)
    "revisit_sec": [8, 25],              # сколько пробыть на вкладке при возврате
    "revisit_click_chance": 0.4,         # вероятность перейти там по внутренней ссылке
    "final_round": True,                 # в конце вернуться ко всем открытым вкладкам
}

# Органические ссылки на домен в выдаче (без рекламы).
RESULT_JS = r"""(() => {
  const dom = %DOMAIN%;
  const isAd = el => !!el.closest('#tads, #bottomads, [data-text-ad], [aria-label*="Реклама"], [aria-label*="Sponsored"], .serp-adv, [data-fast-name="direct"], .Organic_withAdvLabel');
  const host = a => { try { return new URL(a.href).hostname; } catch (e) { return ''; } };
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const r = a.getBoundingClientRect();
    if (r.width < 20 || r.height < 8 || isAd(a)) continue;
    const h = host(a), text = (a.innerText || '').toLowerCase();
    const direct = h === dom || h.endsWith('.' + dom);
    const viaEngine = /google\.|ya\.ru|yandex\./.test(h) && (text.includes(dom) || decodeURIComponent(a.href).includes(dom));
    if ((direct || viaEngine) && text.trim().length > 3) out.push(a);
  }
  return out.slice(0, 5);
})()"""

# Внутренние ссылки сайта, похожие на материалы (не вход, не корзина, не реклама).
INTERNAL_JS = r"""(() => {
  const dom = %DOMAIN%;
  const bad = /(login|signin|sign-in|auth|register|signup|passport|cart|basket|checkout|order|logout|account|profile|subscribe|oauth|id\.)/i;
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    let u; try { u = new URL(a.href); } catch (e) { continue; }
    if (!(u.hostname === dom || u.hostname.endsWith('.' + dom))) continue;
    if (u.pathname.length < 2 || bad.test(u.href) || a.target === '_blank') continue;
    const r = a.getBoundingClientRect();
    if (r.width < 40 || r.height < 10) continue;
    if ((a.innerText || '').trim().length < 12) continue;
    if (a.closest('[class*="adv"], [class*="banner"], [id*="adfox"]')) continue;
    out.push(a);
  }
  return out;
})()"""


def _load() -> dict:
    """Список сайтов: локальный <корень>/warmup/sites.json важнее встроенного."""
    local = paths.root() / "warmup" / "sites.json"
    src = local if local.exists() else BUILTIN
    return json.loads(src.read_text(encoding="utf-8"))


def load_sites() -> list[dict]:
    return [s for s in _load()["sites"] if s.get("enabled", True)]


def load_timing() -> dict:
    return {**TIMING, **(_load().get("timing") or {})}


def pick_sites(count: int, include_yandex: bool, only: list[str] | None = None) -> list[dict]:
    sites = load_sites()
    if only:
        wanted = {d.lower() for d in only}
        return [s for s in sites if s["domain"] in wanted]
    if not include_yandex:
        sites = [s for s in sites if not s.get("yandex")]
    random.shuffle(sites)
    return sites[:count]


class Stopped(Exception):
    pass


class Runner:
    def __init__(self, port: int, on_progress: Callable[[dict], None], stop: asyncio.Event,
                 journal: Callable[[str], None] = lambda s: None):
        self.port = port
        self.on_progress = on_progress
        self.stop = stop
        self.journal = journal
        self.state: dict = {"status": "running", "done": 0, "total": 0, "current": None, "results": [],
                            "started": time.time()}
        self.t = load_timing()
        self.work_tab: str | None = None   # вкладка для поиска
        self.kept: list[dict] = []          # открытые сайты: {"tab", "domain"}
        self.in_revisit = False

    def rng(self, key: str) -> tuple[float, float]:
        a, b = self.t[key]
        return float(a), float(b)

    # ---------------------------------------------------------- утилиты

    def _emit(self) -> None:
        self.on_progress(dict(self.state))

    async def pause(self, a: float, b: float) -> None:
        end = time.monotonic() + random.uniform(a, b)
        while time.monotonic() < end:
            if self.stop.is_set():
                raise Stopped()
            await asyncio.sleep(min(0.5, end - time.monotonic()))

    async def url(self, tab: str | None = None) -> str:
        async with Session(self.port, tab) as s:
            return await s.eval("location.href")

    def tab_ids(self) -> list[str]:
        return [t["id"] for t in browser.tabs(self.port)]

    @staticmethod
    def is_captcha(url: str, title: str = "") -> bool:
        u = url.lower()
        return any(m in u for m in CAPTCHA_MARKERS) or "робот" in title.lower() or "not a robot" in title.lower()

    async def wait_url(self, pred: Callable[[str], bool], timeout: float = 20, tab: str | None = None) -> str:
        end = time.monotonic() + timeout
        url = ""
        while time.monotonic() < end:
            try:
                url = await self.url(tab)
                if pred(url):
                    return url
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        return url

    async def wait_loaded(self, tab: str | None = None) -> None:
        """Ждёт document.readyState == complete (до page_load_timeout_sec) и делает паузу after_load_sec."""
        end = time.monotonic() + float(self.t["page_load_timeout_sec"])
        while time.monotonic() < end:
            if self.stop.is_set():
                raise Stopped()
            try:
                async with Session(self.port, tab) as s:
                    if await s.eval("document.readyState") == "complete":
                        break
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        await self.pause(*self.rng("after_load_sec"))

    async def wheel(self, tab: str | None) -> None:
        """Серия «щелчков» колеса мыши — как прокрутка живого человека."""
        up = random.random() < float(self.t["scroll_up_chance"])
        ticks = random.randint(int(self.t["scroll_ticks"][0]), int(self.t["scroll_ticks"][1]))
        async with Session(self.port, tab) as s:
            vp = await s.eval("[innerWidth, innerHeight]")
            x, y = vp[0] * random.uniform(0.3, 0.7), vp[1] * random.uniform(0.3, 0.7)
            for _ in range(ticks):
                px = random.uniform(*self.rng("scroll_tick_px")) * (-1 if up else 1)
                await s.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": px})
                await asyncio.sleep(random.uniform(0.04, 0.12))

    async def revisit(self) -> None:
        """Возвращается к одной из открытых вкладок: листает, иногда переходит по ссылке, возвращается назад."""
        if not self.kept or self.in_revisit:
            return
        self.in_revisit = True
        back = self.tab_ids()[0] if self.tab_ids() else None
        k = random.choice(self.kept)
        try:
            if k["tab"] not in self.tab_ids():
                self.kept.remove(k)
                return
            browser.activate_tab(self.port, k["tab"])
            self.state["revisits"] = self.state.get("revisits", 0) + 1
            self._emit()
            await self.pause(1, 3)
            end = time.monotonic() + random.uniform(*self.rng("revisit_sec"))
            clicked = random.random() >= float(self.t["revisit_click_chance"])  # True = клик уже «не нужен»
            while time.monotonic() < end:
                await self.wheel(k["tab"])
                await self.pause(*self.rng("scroll_pause_sec"))
                if not clicked and time.monotonic() < end - 5:
                    clicked = True
                    async with Session(self.port, k["tab"]) as s:
                        links = await s.find(INTERNAL_JS.replace("%DOMAIN%", json.dumps(k["domain"])))
                        ok = bool(links) and await s.click_object(random.choice(links[:12]))
                    if ok:
                        await asyncio.sleep(1)
                        await self.wait_loaded(k["tab"])
        except Stopped:
            raise
        except Exception as e:  # noqa: BLE001
            log.info("возврат к вкладке: %s", e)
        finally:
            self.in_revisit = False
            if back and back in self.tab_ids():
                try:
                    browser.activate_tab(self.port, back)
                except Exception:  # noqa: BLE001
                    pass

    def keep_or_close(self, tab: str | None, domain: str) -> None:
        """После сайта: оставить его во вкладке (для возвратов) или закрыть/освободить."""
        if not tab:
            return
        keep = len(self.kept) < int(self.t["keep_tabs_max"]) and random.random() < float(self.t["keep_tab_chance"])
        if keep:
            self.kept.append({"tab": tab, "domain": domain})
            if tab == self.work_tab:
                self.work_tab = None  # следующий поиск — в новой вкладке
        elif tab != self.work_tab and len(self.tab_ids()) > 1:
            try:
                browser.close_tab(self.port, tab)
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------- шаги

    def ensure_work_tab(self) -> str:
        ids = self.tab_ids()
        if not self.work_tab or self.work_tab not in ids or any(k["tab"] == self.work_tab for k in self.kept):
            free = [t for t in ids if not any(k["tab"] == t for k in self.kept)]
            self.work_tab = free[0] if free else browser.new_tab(self.port)["id"]
        browser.activate_tab(self.port, self.work_tab)
        return self.work_tab

    async def search(self, engine: str, query: str) -> str:
        e = SEARCH[engine]
        tab = self.ensure_work_tab()
        await browser.navigate(self.port, e["home"], tab)
        await self.wait_loaded(tab)
        async with Session(self.port, tab) as s:
            fields = await s.find(f"[...document.querySelectorAll({json.dumps(e['input'])})].filter(x => x.offsetParent)")
            if not fields:
                return await s.eval("location.href")
            await s.click_object(fields[0])
            for ch in query:  # печатаем по символу, с разной скоростью
                await s.send("Input.insertText", {"text": ch})
                await asyncio.sleep(random.uniform(*self.rng("typing_delay_sec")))
            await asyncio.sleep(random.uniform(0.3, 0.9))
            await s.key("enter")
        url = await self.wait_url(lambda u: "search" in u or "text=" in u or self.is_captcha(u), 15, tab)
        await self.wait_loaded(tab)
        return url

    async def open_result(self, domain: str) -> str | None:
        """Кликает по органической ссылке на домен. Возвращает id вкладки, где открылся сайт."""
        before = set(self.tab_ids())
        work = self.work_tab
        if random.random() < 0.5:
            await self.wheel(work)  # пробежать выдачу глазами
        await self.pause(0.8, 2.5)
        async with Session(self.port, work) as s:
            links = await s.find(RESULT_JS.replace("%DOMAIN%", json.dumps(domain)))
            if not links:
                return None
            if not await s.click_object(links[0]):
                return None
        # Яндекс открывает результаты в новой вкладке
        await asyncio.sleep(1.5)
        new = [t for t in self.tab_ids() if t not in before]
        if new:
            browser.activate_tab(self.port, new[0])
            await self.wait_url(lambda u: domain in u or self.is_captcha(u), 20, new[0])
            return new[0]
        url = await self.wait_url(lambda u: domain in u or self.is_captcha(u), 20, work)
        return None if domain not in url and not self.is_captcha(url) else work

    async def browse(self, site: dict, tab: str | None) -> dict:
        lo, hi = site.get("dwell_sec") or [40, 60]
        budget = random.uniform(lo, hi)
        t0 = time.monotonic()
        end = t0 + budget
        pages = 1
        n_jumps = random.randint(int(self.t["internal_pages"][0]), int(self.t["internal_pages"][1]))
        jumps = sorted(budget * random.uniform(0.25, 0.65) for _ in range(n_jumps))
        while time.monotonic() < end:
            await self.wheel(tab)
            if self.kept and random.random() < float(self.t["revisit_in_browse_chance"]):
                await self.revisit()
                if tab:
                    browser.activate_tab(self.port, tab)
                end += 5  # время на другой вкладке не съедает время этого сайта
            if random.random() < float(self.t["reading_pause_chance"]):
                await self.pause(*self.rng("reading_pause_sec"))
            else:
                await self.pause(*self.rng("scroll_pause_sec"))
            elapsed = time.monotonic() - t0
            if jumps and elapsed >= jumps[0] and time.monotonic() < end - min(10, budget * 0.25):
                jumps.pop(0)
                async with Session(self.port, tab) as s:
                    links = await s.find(INTERNAL_JS.replace("%DOMAIN%", json.dumps(site["domain"])))
                    clicked = bool(links) and await s.click_object(random.choice(links[:12]))
                if clicked:
                    pages += 1
                    await asyncio.sleep(1)
                    await self.wait_loaded(tab)
                    if self.is_captcha(await self.url(tab)):
                        return {"pages": pages, "captcha": True}
        return {"pages": pages, "captcha": False}

    async def visit(self, site: dict, engine: str) -> dict:
        t0 = time.monotonic()
        res = {"domain": site["domain"], "name": site["name"], "via": engine}
        url = await self.search(engine, site.get("query") or site["domain"])
        if self.is_captcha(url):
            res.update(result="captcha", where=f"поиск {engine}")
            return res
        tab = await self.open_result(site["domain"])
        if tab is None:
            # в выдаче не нашли — заходим напрямую
            res["via"] = "прямой заход"
            tab = self.ensure_work_tab()
            await browser.navigate(self.port, site["url"], tab)
        await self.wait_loaded(tab)
        u = await self.url(tab)
        if self.is_captcha(u):
            res.update(result="captcha", where=site["domain"])
        else:
            b = await self.browse(site, tab)
            res.update(result="captcha" if b["captcha"] else "ok", pages=b["pages"])
        if res.get("result") == "ok":
            self.keep_or_close(tab, site["domain"])
        elif tab and tab != self.work_tab:
            try:
                browser.close_tab(self.port, tab)
            except Exception:  # noqa: BLE001
                pass
        res["sec"] = round(time.monotonic() - t0)
        return res

    async def run(self, sites: list[dict], engine: str) -> dict:
        self.state["total"] = len(sites)
        self._emit()
        try:
            for site in sites:
                if self.stop.is_set():
                    raise Stopped()
                self.state["current"] = site["domain"]
                self._emit()
                try:
                    r = await self.visit(site, engine)
                except Stopped:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.warning("прогрев %s: %s", site["domain"], e)
                    r = {"domain": site["domain"], "name": site["name"], "result": "error", "error": str(e)[:200]}
                self.state["results"].append(r)
                self.state["done"] += 1
                self.journal(f"прогрев: {r['domain']}\t{r['result']}\t{r.get('via', '')}\t"
                             f"{r.get('pages', 0)} стр.\t{r.get('sec', 0)} с")
                self._emit()
                if self.state["done"] < self.state["total"]:
                    if self.kept and random.random() < float(self.t["revisit_chance"]):
                        await self.revisit()
                    await self.pause(*self.rng("between_sites_sec"))
            if self.t.get("final_round") and self.kept:
                self.state["current"] = "возврат к открытым вкладкам"
                self._emit()
                for _ in list(self.kept):
                    await self.revisit()
                    await self.pause(*self.rng("between_sites_sec"))
            self.state["status"] = "done"
        except Stopped:
            self.state["status"] = "stopped"
        self.state["current"] = None
        self.state["kept_tabs"] = [k["domain"] for k in self.kept]
        self.state["finished"] = time.time()
        self._emit()
        return self.state
