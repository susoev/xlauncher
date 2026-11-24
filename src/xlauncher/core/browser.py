"""Управление браузером профиля для агентов через DevTools Protocol (ARCHITECTURE.md §8.3).

Принципы:
  * на странице не остаётся следов: элементы адресуются по backendNodeId DevTools, а не
    атрибутами в DOM; скрипты выполняются в изолированном мире, невидимом для страницы;
    Runtime.enable не вызывается (по нему сайты распознают автоматизацию);
  * клики и ввод — настоящие события мыши и клавиатуры (Input.*), а не element.click().
"""

from __future__ import annotations

import asyncio
import base64
import json
import urllib.parse
import urllib.request

import websockets

ISOLATED_WORLD = "xlauncher-agent"
MAX_ELEMENTS = 150

# Находит видимые интерактивные элементы. Выполняется в изолированном мире.
COLLECT_JS = r"""(() => {
  const SEL = 'a[href], button, input:not([type=hidden]), textarea, select, summary, [contenteditable=""], [contenteditable=true],' +
              '[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], [role=option],' +
              '[role=textbox], [role=combobox], [role=switch], [role=searchbox], [onclick], [tabindex]:not([tabindex="-1"])';
  const vw = innerWidth, vh = innerHeight, out = [], seen = new Set();
  for (const el of document.querySelectorAll(SEL)) {
    if (seen.has(el)) continue; seen.add(el);
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) continue;
    const inView = r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;
    out.push([el, inView, r.top]);
  }
  out.sort((a, b) => (b[1] - a[1]) || (a[2] - b[2]));
  return out.slice(0, %MAX%).map(x => x[0]);
})()""".replace("%MAX%", str(MAX_ELEMENTS))

DESCRIBE_FN = r"""function () {
  const el = this, tag = el.tagName.toLowerCase();
  const role = el.getAttribute('role') || ({a: 'ссылка', button: 'кнопка', select: 'список', textarea: 'поле', summary: 'раскрыть'}[tag])
    || (tag === 'input' ? ({checkbox: 'флажок', radio: 'переключатель', submit: 'кнопка', button: 'кнопка'}[el.type] || 'поле') : tag);
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const label = el.labels && el.labels[0] ? clean(el.labels[0].innerText) : '';
  const isToggle = tag === 'input' && ['checkbox', 'radio'].includes(el.type);
  let name = clean(el.getAttribute('aria-label')) || clean(el.innerText).slice(0, 80) || label || clean(el.getAttribute('placeholder'))
    || clean(el.getAttribute('title')) || clean(el.getAttribute('alt'))
    || (!isToggle && el.value ? clean(String(el.value).slice(0, 40)) : '');
  const r = el.getBoundingClientRect();
  const extra = [];
  if (tag === 'input' && el.type && !['checkbox', 'radio', 'submit', 'button'].includes(el.type)) extra.push(el.type);
  if ((tag === 'input' || tag === 'textarea') && el.value && !['checkbox', 'radio', 'submit', 'button', 'password'].includes(el.type))
    extra.push('значение: ' + String(el.value).slice(0, 40));
  if (el.checked) extra.push('отмечен');
  if (el.disabled) extra.push('недоступен');
  if (tag === 'a' && el.href) extra.push(el.href.slice(0, 100));
  const inView = r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
  return {role, name, extra, inView};
}"""

CENTER_FN = r"""function () {
  // Точка, где под курсором действительно этот элемент. У многострочной ссылки центр общего
  // прямоугольника часто попадает в промежуток между строками — там уже родительский блок.
  this.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const hits = (x, y) => { const h = document.elementFromPoint(x, y); return h && (h === this || this.contains(h)); };
  let rects = [...this.getClientRects()].filter(r => r.width > 1 && r.height > 1);
  if (!rects.length) rects = [this.getBoundingClientRect()];
  rects.sort((a, b) => b.width * b.height - a.width * a.height);
  for (const r of rects) {
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    for (const [x, y] of [[cx, cy], [r.left + Math.min(12, r.width / 2), cy], [cx, r.top + Math.min(8, r.height / 2)]]) {
      if (x >= 0 && y >= 0 && x < innerWidth && y < innerHeight && hits(x, y)) return {x, y, w: r.width, h: r.height, hit: true};
    }
  }
  const r = rects[0];
  return {x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height, hit: false};
}"""

KEYS = {
    "enter": ("Enter", "Enter", 13, "\r"), "tab": ("Tab", "Tab", 9, None), "escape": ("Escape", "Escape", 27, None),
    "backspace": ("Backspace", "Backspace", 8, None), "delete": ("Delete", "Delete", 46, None),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, None), "arrowup": ("ArrowUp", "ArrowUp", 38, None),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37, None), "arrowright": ("ArrowRight", "ArrowRight", 39, None),
    "pagedown": ("PageDown", "PageDown", 34, None), "pageup": ("PageUp", "PageUp", 33, None),
    "home": ("Home", "Home", 36, None), "end": ("End", "End", 35, None), "space": (" ", "Space", 32, " "),
}


class BrowserError(Exception):
    pass


def _http(port: int, path: str, method: str = "GET") -> object:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with opener.open(req, timeout=10) as r:
        body = r.read()
    try:
        return json.loads(body)
    except ValueError:
        return body.decode(errors="replace")


def tabs(port: int) -> list[dict]:
    items = _http(port, "/json/list")
    return [{"id": t["id"], "title": t.get("title", ""), "url": t.get("url", "")}
            for t in items if t.get("type") == "page" and not t.get("url", "").startswith("devtools://")]


def _target(port: int, tab: str | None) -> dict:
    items = [t for t in _http(port, "/json/list") if t.get("type") == "page"]
    if not items:
        raise BrowserError("в браузере нет открытых вкладок")
    if tab:
        for t in items:
            if t["id"] == tab or t["id"].startswith(tab):
                return t
        raise BrowserError(f"вкладка {tab} не найдена")
    return items[0]  # последняя активная


def new_tab(port: int, url: str = "about:blank") -> dict:
    t = _http(port, "/json/new?" + urllib.parse.quote(url, safe=":/?&=%#"), method="PUT")
    return {"id": t["id"], "title": t.get("title", ""), "url": t.get("url", "")}


def activate_tab(port: int, tab: str) -> None:
    _http(port, f"/json/activate/{_target(port, tab)['id']}")


def close_tab(port: int, tab: str) -> None:
    _http(port, f"/json/close/{_target(port, tab)['id']}")


class Session:
    """Подключение к одной вкладке на время одного действия."""

    def __init__(self, port: int, tab: str | None = None):
        self.port, self.tab = port, tab
        self._id = 0
        self.ws = None
        self.target: dict = {}

    async def __aenter__(self) -> "Session":
        self.target = _target(self.port, self.tab)
        self.ws = await websockets.connect(self.target["webSocketDebuggerUrl"], max_size=None, proxy=None)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.ws.close()

    async def send(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), max(0.1, deadline - loop.time())))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise BrowserError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})

    async def world(self) -> int:
        tree = await self.send("Page.getFrameTree")
        res = await self.send("Page.createIsolatedWorld",
                              {"frameId": tree["frameTree"]["frame"]["id"], "worldName": ISOLATED_WORLD})
        return res["executionContextId"]

    async def eval(self, expr: str, by_value: bool = True, ctx: int | None = None):
        params = {"expression": expr, "returnByValue": by_value, "awaitPromise": True}
        if ctx is None:
            ctx = await self.world()
        params["contextId"] = ctx
        res = await self.send("Runtime.evaluate", params)
        if "exceptionDetails" in res:
            raise BrowserError(res["exceptionDetails"].get("text", "ошибка скрипта"))
        return res["result"].get("value") if by_value else res["result"]

    async def call_on(self, object_id: str, fn: str):
        res = await self.send("Runtime.callFunctionOn", {"objectId": object_id, "functionDeclaration": fn,
                                                         "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in res:
            raise BrowserError(res["exceptionDetails"].get("text", "ошибка скрипта"))
        return res["result"].get("value")

    async def resolve(self, ref: str | int) -> str:
        try:
            backend = int(str(ref).lstrip("e"))
        except ValueError as e:
            raise BrowserError(f"неверная ссылка на элемент: {ref}") from e
        try:
            res = await self.send("DOM.resolveNode", {"backendNodeId": backend,
                                                      "executionContextId": await self.world()})
        except BrowserError as e:
            raise BrowserError(f"элемент {ref} не найден — страница изменилась, сделай снимок заново") from e
        return res["object"]["objectId"]

    async def scale(self) -> float:
        """Во сколько раз координаты события на странице отличаются от переданных в Input.*.

        На Windows с масштабом экрана 125–150 % они могут не совпадать. Двигаем мышь в известную
        точку и читаем clientX/Y слушателем в изолированном мире (страница его не видит).
        """
        if getattr(self, "_scale", None):
            return self._scale
        ctx = await self.world()
        await self.eval("window.__xlm = null; document.addEventListener('mousemove', "
                        "e => { window.__xlm = [e.clientX, e.clientY]; }, {capture: true, once: true}); 1", ctx=ctx)
        await self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 200, "y": 200})
        got = None
        for _ in range(10):
            await asyncio.sleep(0.03)
            got = await self.eval("window.__xlm", ctx=ctx)
            if got:
                break
        ratio = (got[0] / 200) if got and got[0] else 1.0
        self._scale = ratio if 0.3 < ratio < 4 else 1.0
        return self._scale

    async def mouse_click(self, x: float, y: float) -> None:
        k = await self.scale()
        x, y = x / k, y / k
        await self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await self.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left",
                                                     "clickCount": 1})
        await self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left",
                                                     "clickCount": 1})

    async def key(self, name: str) -> None:
        k = KEYS.get(name.lower().replace(" ", ""))
        if not k:
            if len(name) == 1:
                await self.send("Input.insertText", {"text": name})
                return
            raise BrowserError(f"неизвестная клавиша {name}; доступны: {', '.join(sorted(KEYS))}")
        key, code, vk, text = k
        base = {"key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
        await self.send("Input.dispatchKeyEvent", {"type": "keyDown" if text is None else "keyDown", **base,
                                                   **({"text": text} if text else {})})
        await self.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})


# ---------------------------------------------------------------- действия


async def _wait_ready(s: Session, timeout: float = 30) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            if await s.eval("document.readyState") == "complete":
                return
        except BrowserError:
            pass
        await asyncio.sleep(0.3)


async def snapshot(port: int, tab: str | None = None, text_limit: int = 3000) -> dict:
    async with Session(port, tab) as s:
        ctx = await s.world()
        arr = await s.eval(COLLECT_JS, by_value=False, ctx=ctx)
        props = await s.send("Runtime.getProperties", {"objectId": arr["objectId"], "ownProperties": True})
        elements = []
        for p in props["result"]:
            if not p["name"].isdigit() or "objectId" not in p.get("value", {}):
                continue
            oid = p["value"]["objectId"]
            node = await s.send("DOM.describeNode", {"objectId": oid})
            info = await s.call_on(oid, DESCRIBE_FN)
            elements.append({"ref": f"e{node['node']['backendNodeId']}", **info})
        page = await s.eval("({title: document.title, url: location.href, "
                            "text: (document.body ? document.body.innerText : '').replace(/\\n{3,}/g, '\\n\\n'),"
                            "scroll: [scrollY, document.documentElement.scrollHeight, innerHeight]})", ctx=ctx)
    text = page["text"]
    return {"tab": s.target["id"], "title": page["title"], "url": page["url"],
            "text": text[:text_limit] + ("…" if len(text) > text_limit else ""),
            "scroll": {"y": page["scroll"][0], "height": page["scroll"][1], "viewport": page["scroll"][2]},
            "elements": elements}


def format_snapshot(snap: dict) -> str:
    lines = [f"Вкладка {snap['tab'][:8]}: {snap['title']}", snap["url"],
             f"Прокрутка: {snap['scroll']['y']} из {snap['scroll']['height']} (окно {snap['scroll']['viewport']})", "",
             "Элементы (ссылки для click/type):"]
    for e in snap["elements"]:
        extra = f" ({'; '.join(e['extra'])})" if e["extra"] else ""
        off = "" if e["inView"] else " [вне экрана]"
        lines.append(f"[{e['ref']}] {e['role']} «{e['name']}»{extra}{off}")
    lines += ["", "Текст страницы:", snap["text"]]
    return "\n".join(lines)


async def navigate(port: int, url: str, tab: str | None = None, referrer: str | None = None) -> dict:
    if "://" not in url and not url.startswith("about:"):
        url = "https://" + url
    async with Session(port, tab) as s:
        params = {"url": url}
        if referrer:
            params["referrer"] = referrer
        res = await s.send("Page.navigate", params)
        if res.get("errorText"):
            raise BrowserError(f"не удалось открыть {url}: {res['errorText']}")
        await asyncio.sleep(0.3)
        await _wait_ready(s)
        return {"url": await s.eval("location.href"), "title": await s.eval("document.title")}


async def click(port: int, ref: str, tab: str | None = None) -> dict:
    async with Session(port, tab) as s:
        oid = await s.resolve(ref)
        c = await s.call_on(oid, CENTER_FN)
        if c["w"] < 1 or c["h"] < 1:
            raise BrowserError(f"элемент {ref} невидим")
        await asyncio.sleep(0.15)
        c = await s.call_on(oid, CENTER_FN)
        await s.mouse_click(c["x"], c["y"])
        await asyncio.sleep(0.8)
        return {"clicked": ref, "url": await s.eval("location.href"), "scale": round(await s.scale(), 3)}


async def type_text(port: int, ref: str, text: str, submit: bool = False, clear: bool = True,
                    tab: str | None = None) -> dict:
    async with Session(port, tab) as s:
        oid = await s.resolve(ref)
        c = await s.call_on(oid, CENTER_FN)
        await s.mouse_click(c["x"], c["y"])
        if clear:
            await s.call_on(oid, "function () { if (this.select) { this.select(); } else { "
                                 "const r = document.createRange(); r.selectNodeContents(this); "
                                 "const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); } }")
            await s.key("backspace")
        await s.send("Input.insertText", {"text": text})
        if submit:
            await s.key("enter")
            await asyncio.sleep(0.5)
        return {"typed": len(text), "ref": ref}


async def press_key(port: int, key: str, tab: str | None = None) -> dict:
    async with Session(port, tab) as s:
        await s.key(key)
        await asyncio.sleep(0.2)
        return {"key": key}


async def scroll(port: int, dy: int = 600, tab: str | None = None) -> dict:
    async with Session(port, tab) as s:
        vp = await s.eval("[innerWidth, innerHeight]")
        await s.send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": vp[0] / 2, "y": vp[1] / 2,
                                                  "deltaX": 0, "deltaY": dy})
        await asyncio.sleep(0.4)
        return {"y": await s.eval("scrollY")}


async def screenshot(port: int, tab: str | None = None, quality: int = 70) -> bytes:
    async with Session(port, tab) as s:
        res = await s.send("Page.captureScreenshot", {"format": "jpeg", "quality": quality})
        return base64.b64decode(res["data"])


async def wait_for(port: int, text: str | None = None, seconds: float = 2, tab: str | None = None) -> dict:
    if not text:
        await asyncio.sleep(min(seconds, 30))
        return {"waited": seconds}
    loop = asyncio.get_running_loop()
    end = loop.time() + min(seconds, 60)
    async with Session(port, tab) as s:
        while loop.time() < end:
            body = await s.eval("document.body ? document.body.innerText : ''")
            if text in body:
                return {"found": True}
            await asyncio.sleep(0.5)
    return {"found": False}
