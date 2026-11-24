"""MCP-сервер XLauncher: python -m xlauncher.mcp_server (stdio).

Запускается через загрузчик: C:\\xl\\xlauncher.exe --mcp. Сам ничего не хранит — вызывает
HTTP API работающего приложения на 127.0.0.1 (порт из run/app.json). Если приложение не
запущено, запускает его (ARCHITECTURE.md §8.2, §8.3).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

INSTRUCTIONS = """Управление профилями браузера XLauncher на компьютере пользователя.
Каждый профиль — отдельный браузер со своими куками, отпечатком и прокси (обычно аккаунт клиента в соцсети).

Порядок работы:
1. list_profiles — найти профиль (по имени, клиенту или id).
2. start_profile — запустить его с доступом агента. Если профиль уже открыт пользователем без доступа агента,
   спроси пользователя, можно ли перезапустить (restart=true).
3. browser_snapshot — прочитать страницу: текст и список элементов со ссылками вида [e123].
4. browser_click / browser_type / browser_press_key / browser_scroll — действовать по ссылкам из последнего снимка.
   После действий, которые меняют страницу, делай новый снимок: ссылки на элементы устаревают.
5. browser_screenshot — если нужна картинка (капча, вёрстка, медиа).

Правила: это реальные аккаунты клиентов. Не публикуй, не отправляй сообщения, не меняй настройки аккаунта и не
удаляй ничего без явного подтверждения пользователя. Не вводи пароли и коды — попроси пользователя сделать это.
Действуй в темпе человека, без массовых операций. Все действия записываются в журнал профиля."""

mcp = MCPServer("xlauncher", instructions=INSTRUCTIONS)


# ---------------------------------------------------------------- связь с приложением


def _root() -> Path:
    env = os.environ.get("XL_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[4]  # <корень>/app/<версия>/src/xlauncher/


def _port() -> int | None:
    try:
        return int(json.loads((_root() / "run" / "app.json").read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError):
        return None


def _request(method: str, path: str, body: dict | None = None, timeout: float = 120) -> dict:
    port = _port()
    if not port:
        raise ToolError("XLauncher не запущен")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except ValueError:
            msg = None
        raise ToolError(msg or f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise ToolError(f"нет связи с XLauncher: {e.reason}") from None


def _ensure_app() -> None:
    """Если приложение не отвечает — запускает xlauncher.exe и ждёт до 40 с."""
    try:
        _request("GET", "/api/health", timeout=3)
        return
    except Exception:  # noqa: BLE001
        pass
    exe = _root() / "xlauncher.exe"
    if not exe.exists():
        raise ToolError(f"XLauncher не запущен и не найден {exe}")
    flags = 0x00000008 if sys.platform == "win32" else 0  # DETACHED_PROCESS
    subprocess.Popen([str(exe)], creationflags=flags, close_fds=True)
    for _ in range(80):
        time.sleep(0.5)
        try:
            _request("GET", "/api/health", timeout=2)
            return
        except Exception:  # noqa: BLE001
            continue
    raise ToolError("XLauncher не запустился за 40 секунд")


def api(method: str, path: str, body: dict | None = None, timeout: float = 120) -> dict:
    _ensure_app()
    return _request(method, path, body, timeout)


def _resolve(profile: str) -> dict:
    """Профиль по id, точному имени или однозначной части имени/клиента."""
    items = api("GET", "/api/profiles")["profiles"]
    q = profile.strip().lower()
    for p in items:
        if p["id"].lower() == q or p["name"].lower() == q:
            return p
    hits = [p for p in items if q in p["name"].lower() or q in (p.get("group") or "").lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ToolError(f"профиль «{profile}» не найден; вызови list_profiles")
    raise ToolError("несколько профилей подходят: " + "; ".join(f"{p['name']} ({p['id']})" for p in hits[:10]))


def _status_line(p: dict) -> str:
    st = p.get("state") or {}
    s = st.get("status") or "не запущен"
    ip = st.get("ip") or (p.get("last_ip") or {}).get("ip") or "—"
    country = ((st.get("geo") or {}).get("country") if st.get("ip") else (p.get("last_ip") or {}).get("country")) or ""
    agent = ", доступ агента" if st.get("cdp_port") else ""
    return f"{p['name']} [{p.get('group') or 'без клиента'}] id={p['id']} статус={s}{agent} IP={ip} {country}".strip()


# ---------------------------------------------------------------- профили


@mcp.tool()
def list_profiles(query: str = "") -> str:
    """Список профилей с клиентом, статусом и последним IP. query — фильтр по имени или клиенту."""
    items = api("GET", "/api/profiles")["profiles"]
    q = query.strip().lower()
    items = [p for p in items if not q or q in p["name"].lower() or q in (p.get("group") or "").lower()]
    return "\n".join(_status_line(p) for p in items) or "Профилей нет"


@mcp.tool()
def profile_status(profile: str) -> str:
    """Подробный статус профиля: прокси, IP, страна, предупреждения, ошибки последнего запуска."""
    p = _resolve(profile)
    p = api("GET", f"/api/profiles/{p['id']}")
    st = p.get("state") or {}
    lines = [_status_line(p), f"прокси: {p.get('proxy_masked') or 'не задан'}"]
    for k in ("error", "warning"):
        if st.get(k):
            lines.append(f"{k}: {st[k]}")
    if st.get("timezone"):
        lines.append(f"часовой пояс: {st['timezone']}")
    return "\n".join(lines)


@mcp.tool()
def start_profile(profile: str, restart: bool = False, confirm_country: bool = False) -> str:
    """Запускает профиль с доступом агента и ждёт, пока браузер откроется (до 90 с).

    restart=true — если профиль уже открыт без доступа агента, закрыть и открыть заново (спроси пользователя).
    confirm_country=true — запустить, даже если страна IP прокси сменилась (только с согласия пользователя).
    """
    p = _resolve(profile)
    pid = p["id"]
    st = p.get("state") or {}
    if st.get("status") == "running":
        if st.get("cdp_port"):
            return f"Уже запущен с доступом агента. {_status_line(p)}"
        if not restart:
            return ("Профиль открыт пользователем без доступа агента. Спроси пользователя, можно ли перезапустить "
                    "браузер, и вызови start_profile с restart=true.")
        api("POST", f"/api/profiles/{pid}/stop")
        for _ in range(60):
            time.sleep(0.5)
            if (api("GET", f"/api/profiles/{pid}").get("state") or {}).get("status") not in ("running", "stopping"):
                break
    r = api("POST", f"/api/profiles/{pid}/start", {"agent": True, "confirm_country": confirm_country})
    if r.get("ok") is False:
        return f"Не запущен: {r.get('error')}"
    for _ in range(180):
        time.sleep(0.5)
        cur = api("GET", f"/api/profiles/{pid}")
        st = cur.get("state") or {}
        s = st.get("status")
        if s == "running" and st.get("cdp_port"):
            time.sleep(1.5)  # браузеру нужна секунда, чтобы открыть вкладки
            return f"Запущен. {_status_line(cur)}"
        if s == "needs_confirmation":
            return f"Не запущен: {st.get('error')}. Спроси пользователя и повтори с confirm_country=true."
        if s in ("error", "crashed"):
            return f"Не запущен: {st.get('error')}"
    return "Браузер не открылся за 90 секунд — проверь профиль в окне XLauncher."


@mcp.tool()
def stop_profile(profile: str) -> str:
    """Закрывает браузер профиля (как крестиком: куки и сессия сохраняются)."""
    p = _resolve(profile)
    api("POST", f"/api/profiles/{p['id']}/stop")
    for _ in range(60):
        time.sleep(0.5)
        st = api("GET", f"/api/profiles/{p['id']}").get("state") or {}
        if st.get("status") not in ("running", "stopping"):
            return f"Остановлен: {p['name']}"
    return "Команда отправлена, браузер ещё закрывается."


@mcp.tool()
def ip_history(profile: str) -> str:
    """История внешних IP профиля (строка появляется только при смене IP)."""
    p = _resolve(profile)
    rows = api("GET", f"/api/profiles/{p['id']}/ip-history")["history"]
    return "\n".join(f"{r['time']}  {r['ip']}  {r['country']} {r['city']}  {r['source']} {r['flag']}".rstrip()
                     for r in rows) or "История пуста"


# ---------------------------------------------------------------- браузер


def _b(profile: str, action: str, **args) -> dict:
    p = _resolve(profile)
    return api("POST", f"/api/profiles/{p['id']}/browser/{action}", {k: v for k, v in args.items() if v is not None})


@mcp.tool()
def browser_tabs(profile: str) -> str:
    """Открытые вкладки браузера профиля. Первая — последняя активная, по умолчанию действия идут в неё."""
    tabs = _b(profile, "tabs")["tabs"]
    return "\n".join(f"{t['id'][:8]}  {t['title']}  {t['url']}" for t in tabs) or "Вкладок нет"


@mcp.tool()
def browser_new_tab(profile: str, url: str = "about:blank") -> str:
    """Открывает новую вкладку."""
    t = _b(profile, "new_tab", url=url)
    return f"Открыта вкладка {t['id'][:8]}: {t['url']}"


@mcp.tool()
def browser_switch_tab(profile: str, tab: str) -> str:
    """Делает вкладку активной (tab — id из browser_tabs, можно первые 8 символов)."""
    _b(profile, "activate_tab", tab=tab)
    return "Готово"


@mcp.tool()
def browser_close_tab(profile: str, tab: str) -> str:
    """Закрывает вкладку."""
    _b(profile, "close_tab", tab=tab)
    return "Закрыта"


@mcp.tool()
def browser_navigate(profile: str, url: str, tab: str | None = None) -> str:
    """Открывает адрес во вкладке и ждёт загрузки."""
    r = _b(profile, "navigate", url=url, tab=tab)
    return f"Открыто: {r['title']} — {r['url']}"


@mcp.tool()
def browser_snapshot(profile: str, tab: str | None = None, text_limit: int = 3000) -> str:
    """Снимок страницы: адрес, текст и интерактивные элементы со ссылками [eNNN] для click/type."""
    return _b(profile, "snapshot", tab=tab, text_limit=text_limit)["text"]


@mcp.tool()
def browser_click(profile: str, ref: str, tab: str | None = None) -> str:
    """Клик мышью по элементу (ref из последнего снимка, например e123)."""
    r = _b(profile, "click", ref=ref, tab=tab)
    return f"Нажато {ref}. Адрес: {r['url']}"


@mcp.tool()
def browser_type(profile: str, ref: str, text: str, submit: bool = False, clear: bool = True,
                 tab: str | None = None) -> str:
    """Вводит текст в поле: клик, очистка (clear), ввод; submit=true — нажать Enter. Пароли не вводи."""
    _b(profile, "type", ref=ref, text=text, submit=submit, clear=clear, tab=tab)
    return f"Введено в {ref}" + (" и отправлено" if submit else "")


@mcp.tool()
def browser_press_key(profile: str, key: str, tab: str | None = None) -> str:
    """Нажимает клавишу: Enter, Tab, Escape, Backspace, Delete, ArrowDown/Up/Left/Right, PageDown/Up, Home, End, Space."""
    _b(profile, "press_key", key=key, tab=tab)
    return f"Нажата {key}"


@mcp.tool()
def browser_scroll(profile: str, dy: int = 600, tab: str | None = None) -> str:
    """Прокрутка колесом мыши: dy > 0 — вниз, dy < 0 — вверх (в пикселях)."""
    r = _b(profile, "scroll", dy=dy, tab=tab)
    return f"Прокручено, позиция {r['y']}"


@mcp.tool()
def browser_screenshot(profile: str, tab: str | None = None) -> Image:
    """Скриншот видимой части страницы (JPEG)."""
    import base64

    r = _b(profile, "screenshot", tab=tab)
    return Image(data=base64.b64decode(r["jpeg_base64"]), format="jpeg")


@mcp.tool()
def browser_wait(profile: str, text: str | None = None, seconds: float = 2, tab: str | None = None) -> str:
    """Ждёт появления текста на странице (до seconds, максимум 60) или просто пауза, если text не задан."""
    r = _b(profile, "wait", text=text, seconds=seconds, tab=tab)
    if "found" in r:
        return "Текст найден" if r["found"] else "Текст не появился"
    return f"Подождал {seconds} с"


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
