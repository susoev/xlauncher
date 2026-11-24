# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright>=1.49"]
# ///
"""xlauncher — этап 1: проверка рисков на Windows.

Запуск из корня репозитория:  uv run spike\\stage1.py

Что делает:
  1. скачивает fingerprint-chromium (последний релиз для Windows x64);
  2. спрашивает строку прокси, проверяет его через локальный форвардер, определяет гео;
  3. запускает два профиля с разными отпечатками, первый — дважды (проверка стабильности);
  4. собирает отпечаток со страницы: UA, ядра, часовой пояс, WebGL, canvas, audio, WebRTC, IP;
  5. проверяет перенос профиля: ключ шифрования кук, восстановление после «переезда»;
  6. пишет отчёт в spike\\out\\report.md и report.json.

Ничего не устанавливает в систему; всё лежит в spike\\out\\ (в git не попадает).
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import getpass
import io
import json
import os
import platform
import random
import secrets
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import urllib.request
import zipfile
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from forwarder import Forwarder, parse_proxy  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
ENGINES = OUT / "engines"
PROFILES = OUT / "profiles"
CACHE = OUT / "cache"
REPO = "adryfish/fingerprint-chromium"
UA = {"User-Agent": "xlauncher-spike"}

PROBES: dict[str, str] = {
    "basic": r"""async () => {
      const n = navigator;
      return {userAgent: n.userAgent, platform: n.platform, hardwareConcurrency: n.hardwareConcurrency,
        deviceMemory: n.deviceMemory, languages: n.languages, webdriver: n.webdriver,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, tzOffset: new Date().getTimezoneOffset(),
        screen: [screen.width, screen.height, screen.colorDepth, devicePixelRatio]};
    }""",
    "uaData": r"""async () => {
      const n = navigator;
      const h = await n.userAgentData.getHighEntropyValues(['platform','platformVersion','fullVersionList','architecture']);
      return {uaData: {brands: n.userAgentData.brands.map(b => b.brand + ' ' + b.version),
        platform: h.platform, platformVersion: h.platformVersion, architecture: h.architecture,
        fullVersionList: h.fullVersionList.map(b => b.brand + ' ' + b.version)}};
    }""",
    "canvas": r"""async () => {
      const c = document.createElement('canvas'); c.width = 280; c.height = 60;
      const x = c.getContext('2d');
      x.textBaseline = 'top'; x.font = '16px Arial';
      x.fillStyle = '#f60'; x.fillRect(100, 1, 62, 20);
      x.fillStyle = '#069'; x.fillText('xlauncher ✓ 1.0 <canvas>', 2, 15);
      x.fillStyle = 'rgba(102,204,0,0.7)'; x.fillText('xlauncher ✓ 1.0 <canvas>', 4, 17);
      const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(c.toDataURL()));
      return {canvas: [...new Uint8Array(d)].map(v => v.toString(16).padStart(2, '0')).join('').slice(0, 16)};
    }""",
    "webgl": r"""async () => {
      const gl = document.createElement('canvas').getContext('webgl');
      if (!gl) return {webglRenderer: 'webgl недоступен'};
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      return {webglVendor: gl.getParameter(ext.UNMASKED_VENDOR_WEBGL),
              webglRenderer: gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)};
    }""",
    "audio": r"""async () => {
      const ctx = new OfflineAudioContext(1, 44100, 44100);
      const o = ctx.createOscillator(); o.type = 'triangle'; o.frequency.value = 10000;
      const comp = ctx.createDynamicsCompressor();
      o.connect(comp); comp.connect(ctx.destination); o.start(0);
      const buf = await ctx.startRendering();
      let s = 0; for (const v of buf.getChannelData(0).slice(4500, 5000)) s += Math.abs(v);
      return {audio: s.toString()};
    }""",
    "ipSeen": r"""async () => ({ipSeen: (await (await fetch('https://api.ipify.org?format=json')).json()).ip})""",
    "webrtc": r"""async () => ({webrtc: await new Promise(res => {
      const ips = new Set();
      const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:stun.l.google.com:19302'}]});
      pc.createDataChannel('x');
      pc.onicecandidate = ev => {
        if (ev.candidate && ev.candidate.candidate) {
          const m = ev.candidate.candidate.split(' ');
          if (m[4]) ips.add(m[4] + ' ' + (m[7] || ''));
        }
      };
      pc.createOffer().then(o => pc.setLocalDescription(o));
      setTimeout(() => { try { pc.close(); } catch (e) {} res([...ips]); }, 4000);
    })})""",
}


# ---------------------------------------------------------------- утилиты


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_get(url: str, proxy_port: int | None = None, timeout: int = 20) -> bytes:
    """GET напрямую (proxy_port=None) или через локальный форвардер."""
    proxies = {"http": f"http://127.0.0.1:{proxy_port}", "https": f"http://127.0.0.1:{proxy_port}"} if proxy_port else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    with opener.open(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


async def aget(url: str, proxy_port: int | None = None, timeout: int = 20) -> bytes:
    return await asyncio.to_thread(http_get, url, proxy_port, timeout)


async def get_ip(proxy_port: int | None) -> str:
    for url in ("https://api.ipify.org?format=json", "https://ifconfig.me/ip"):
        try:
            data = await aget(url, proxy_port)
            return json.loads(data)["ip"] if data.strip().startswith(b"{") else data.decode().strip()
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"Не удалось получить IP: {last}")


async def get_geo(ip: str, proxy_port: int | None) -> dict:
    try:
        d = json.loads(await aget(f"https://ipwho.is/{ip}", proxy_port))
        if d.get("success"):
            return {
                "country": d.get("country_code"),
                "city": d.get("city"),
                "timezone": (d.get("timezone") or {}).get("id"),
                "asn": f"AS{(d.get('connection') or {}).get('asn')} {(d.get('connection') or {}).get('isp')}",
                "source": "ipwho.is",
            }
    except Exception:  # noqa: BLE001
        pass
    try:
        d = json.loads(await aget(f"http://ip-api.com/json/{ip}?fields=status,countryCode,city,timezone,as", proxy_port))
        if d.get("status") == "success":
            return {"country": d["countryCode"], "city": d["city"], "timezone": d["timezone"], "asn": d["as"], "source": "ip-api.com"}
    except Exception:  # noqa: BLE001
        pass
    return {"country": None, "city": None, "timezone": None, "asn": None, "source": None}


# ---------------------------------------------------------------- DPAPI


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    inp = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = DATA_BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError(f"DPAPI {'protect' if protect else 'unprotect'}: ошибка {ctypes.GetLastError()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def local_state_path(data_dir: Path) -> Path:
    return data_dir / "Local State"


def read_os_crypt(data_dir: Path) -> dict:
    st = json.loads(local_state_path(data_dir).read_text(encoding="utf-8"))
    return st.get("os_crypt", {})


def extract_key(data_dir: Path) -> bytes:
    enc = base64.b64decode(read_os_crypt(data_dir)["encrypted_key"])
    if not enc.startswith(b"DPAPI"):
        raise RuntimeError("encrypted_key без префикса DPAPI")
    return _dpapi(enc[5:], protect=False)


def key_decryptable(data_dir: Path) -> bool:
    try:
        extract_key(data_dir)
        return True
    except Exception:  # noqa: BLE001
        return False


def write_key(data_dir: Path, raw: bytes | None) -> None:
    """raw=None — записать мусор (имитация переезда на другой компьютер)."""
    p = local_state_path(data_dir)
    st = json.loads(p.read_text(encoding="utf-8"))
    blob = _dpapi(raw, protect=True) if raw is not None else os.urandom(230)
    st["os_crypt"]["encrypted_key"] = base64.b64encode(b"DPAPI" + blob).decode()
    p.write_text(json.dumps(st), encoding="utf-8")


def cookie_prefixes(data_dir: Path) -> dict[str, int]:
    db = data_dir / "Default" / "Network" / "Cookies"
    if not db.exists():
        return {}
    tmp = Path(tempfile.mkdtemp()) / "c.db"
    shutil.copy2(db, tmp)
    res: dict[str, int] = {}
    con = sqlite3.connect(tmp)
    try:
        for (v,) in con.execute("select encrypted_value from cookies"):
            k = bytes(v[:3]).decode("latin-1") if v else "(пусто)"
            res[k] = res.get(k, 0) + 1
    finally:
        con.close()
    return res


# ---------------------------------------------------------------- движок


def ensure_engine() -> tuple[Path, str]:
    ENGINES.mkdir(parents=True, exist_ok=True)
    existing = sorted(ENGINES.glob("*/**/chrome.exe"))
    if existing:
        exe = existing[-1]
        ver = exe.relative_to(ENGINES).parts[0]
        log(f"Движок уже скачан: {ver}")
        return exe, ver
    log("Ищу последний релиз fingerprint-chromium…")
    rel = json.loads(http_get(f"https://api.github.com/repos/{REPO}/releases/latest"))
    tag = rel["tag_name"]
    assets = rel.get("assets", [])
    names = [a["name"] for a in assets]

    def pick() -> dict | None:
        for a in assets:
            n = a["name"].lower()
            if n.endswith(".zip") and "win" in n and ("x64" in n or "64" in n or "amd64" in n):
                return a
        for a in assets:
            n = a["name"].lower()
            if n.endswith(".zip") and "win" in n:
                return a
        return None

    asset = pick()
    if not asset:
        raise RuntimeError(f"В релизе {tag} нет zip для Windows. Файлы: {names}")
    log(f"Скачиваю {asset['name']} ({asset['size'] / 1e6:.0f} МБ)…")
    req = urllib.request.Request(asset["browser_download_url"], headers=UA)
    buf = io.BytesIO()
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or asset["size"])
        got, last = 0, 0
        while chunk := r.read(1 << 20):
            buf.write(chunk)
            got += len(chunk)
            if got - last > 20 << 20:
                log(f"  {got / 1e6:.0f} / {total / 1e6:.0f} МБ")
                last = got
    dest = ENGINES / tag
    log("Распаковываю…")
    with zipfile.ZipFile(buf) as z:
        z.extractall(dest)
    exe = next(dest.rglob("chrome.exe"), None)
    if not exe:
        raise RuntimeError("В архиве не найден chrome.exe")
    return exe, tag


# ---------------------------------------------------------------- профиль


class Profile:
    def __init__(self, name: str, seed: int, cores: int, fwd: Forwarder, tz: str | None, lang: str = "en-US"):
        self.name = name
        self.seed = seed
        self.cores = cores
        self.fwd = fwd
        self.tz = tz
        self.lang = lang
        self.data = PROFILES / name / "data"
        self.cache = CACHE / name

    def args(self, cdp_port: int | None) -> list[str]:
        a = [
            f"--user-data-dir={self.data}",
            f"--disk-cache-dir={self.cache}",
            f"--fingerprint={self.seed}",
            "--fingerprint-platform=windows",
            "--fingerprint-brand=Chrome",
            f"--fingerprint-hardware-concurrency={self.cores}",
            f"--lang={self.lang}",
            f"--accept-lang={self.lang},{self.lang.split('-')[0]}",
            f"--proxy-server=http://127.0.0.1:{self.fwd.port}",
            "--disable-non-proxied-udp",
            "--disable-quic",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if self.tz:
            a.append(f"--timezone={self.tz}")
        if cdp_port:
            a += [f"--remote-debugging-port={cdp_port}", "--remote-debugging-address=127.0.0.1"]
        return a


async def wait_cdp(port: int, timeout: float = 30) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            await aget(f"http://127.0.0.1:{port}/json/version", None, 2)
            return
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.5)
    raise RuntimeError("Браузер не открыл порт отладки за 30 с")


async def run_session(exe: Path, prof: Profile, pw, *, collect: bool = True, set_cookie: str | None = None,
                      read_cookie: bool = False) -> dict:
    """Запускает профиль, выполняет действия через CDP, корректно закрывает браузер."""
    port = free_port()
    prof.data.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(str(exe), *prof.args(port), "about:blank")
    res: dict = {}
    try:
        await wait_cdp(port)
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://example.com/", wait_until="load", timeout=45000)
        if set_cookie:
            await page.evaluate(f"document.cookie = 'xl_test={set_cookie}; max-age=31536000; path=/'")
            res["cookie_set"] = await page.evaluate("document.cookie")
        if read_cookie:
            cookies = await ctx.cookies("https://example.com")
            res["cookie"] = next((c["value"] for c in cookies if c["name"] == "xl_test"), None)
        if collect:
            fp: dict = {}
            crashed: list[str] = []
            for name, js in PROBES.items():
                pg = await ctx.new_page()
                try:
                    await pg.goto("https://example.com/", wait_until="load", timeout=45000)
                    fp.update(await asyncio.wait_for(pg.evaluate(js), 30))
                except Exception as e:  # noqa: BLE001
                    msg = str(e)
                    if "crash" in msg.lower():
                        crashed.append(name)
                        fp[name] = "ВКЛАДКА УПАЛА"
                    else:
                        fp[name] = f"err {type(e).__name__}: {msg[:200]}"
                finally:
                    try:
                        await pg.close()
                    except Exception:  # noqa: BLE001
                        pass
            res["fp"] = fp
            res["crashed_probes"] = crashed
        cdp = await browser.new_browser_cdp_session()
        try:
            await cdp.send("Browser.close")
        except Exception:  # noqa: BLE001
            pass
        await asyncio.wait_for(proc.wait(), 20)
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return res


def clear_locks(data_dir: Path) -> None:
    for n in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        p = data_dir / n
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- отчёт


def fp_rows(results: dict[str, dict]) -> str:
    keys = [
        ("userAgent", "User-Agent"),
        ("uaData", "UA Client Hints"),
        ("platform", "navigator.platform"),
        ("hardwareConcurrency", "Ядра"),
        ("deviceMemory", "deviceMemory"),
        ("languages", "Языки"),
        ("timezone", "Часовой пояс"),
        ("screen", "Экран"),
        ("webdriver", "navigator.webdriver"),
        ("webglVendor", "WebGL vendor"),
        ("webglRenderer", "WebGL renderer"),
        ("canvas", "Canvas hash"),
        ("audio", "Audio"),
        ("ipSeen", "IP глазами сайта"),
        ("webrtc", "WebRTC кандидаты"),
    ]
    names = list(results)
    out = ["| Параметр | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for k, title in keys:
        vals = []
        for n in names:
            v = (results[n].get("fp") or {}).get(k, results[n].get("error", "—"))
            s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            vals.append(s.replace("|", "\\|")[:300])
        out.append(f"| {title} | " + " | ".join(vals) + " |")
    return "\n".join(out)


# ---------------------------------------------------------------- main


async def main() -> None:
    if platform.system() != "Windows":
        print("Этот скрипт рассчитан на Windows.")
        return
    OUT.mkdir(exist_ok=True)
    report: dict = {"started": datetime.now().isoformat(timespec="seconds"), "host": platform.node(),
                    "os": platform.platform()}
    print("\n=== xlauncher — этап 1: проверка рисков ===\n")

    exe, ver = ensure_engine()
    report["engine"] = {"version": ver, "exe": str(exe.relative_to(OUT))}

    print("\nВставь строку прокси (ввод скрыт). Форматы: socks5://user:pass@host:port, "
          "http://user:pass@host:port, host:port:user:pass.")
    p1 = getpass.getpass("Прокси для профиля A (Enter — без прокси): ")
    p2 = getpass.getpass("Прокси для профиля B (Enter — тот же): ") or p1
    up1, up2 = parse_proxy(p1), parse_proxy(p2)

    log("Определяю реальный IP (без прокси) для проверки утечек…")
    real_ip = await get_ip(None)
    report["real_ip"] = real_ip

    proxies = {}
    fwds = {}
    for label, up in (("A", up1), ("B", up2)):
        f = Forwarder(up)
        await f.start()
        fwds[label] = f
        log(f"Профиль {label}: проверяю прокси {up.masked() if up else '(нет)'}…")
        t0 = time.monotonic()
        try:
            ip = await get_ip(f.port)
            geo = await get_geo(ip, f.port)
            proxies[label] = {"proxy": up.masked() if up else None, "ok": True, "ip": ip, "geo": geo,
                              "latency_s": round(time.monotonic() - t0, 2)}
            log(f"  OK: {ip} {geo.get('country')} {geo.get('city')} {geo.get('timezone')} {geo.get('asn')}")
        except Exception as e:  # noqa: BLE001
            proxies[label] = {"proxy": up.masked() if up else None, "ok": False, "error": str(e),
                              "forwarder_errors": f.errors[-3:]}
            log(f"  АЛЯРМ: прокси не работает — {e}")
    report["proxies"] = proxies
    if not all(p["ok"] for p in proxies.values()):
        write_report(report)
        log("Прокси не работает — дальше не иду. Отчёт сохранён.")
        return

    from playwright.async_api import async_playwright

    seeds = {"A": random.getrandbits(32), "B": random.getrandbits(32)}
    profA = Profile("A", seeds["A"], random.choice([8, 12, 16]), fwds["A"], proxies["A"]["geo"]["timezone"])
    profB = Profile("B", seeds["B"], random.choice([4, 6, 8]), fwds["B"], proxies["B"]["geo"]["timezone"])
    for p in (profA, profB):
        if (PROFILES / p.name).exists():
            shutil.rmtree(PROFILES / p.name)
        if p.cache.exists():
            shutil.rmtree(p.cache)
    report["profiles"] = {p.name: {"seed": p.seed, "cores": p.cores, "timezone": p.tz, "args": p.args(None)}
                          for p in (profA, profB)}

    cookie_val = secrets.token_hex(8)
    async with async_playwright() as pw:
        log("Профиль A, запуск 1: собираю отпечаток, ставлю тестовую куку…")
        a1 = await run_session(exe, profA, pw, set_cookie=cookie_val)
        log("Профиль B: собираю отпечаток…")
        b1 = await run_session(exe, profB, pw)
        log("Профиль A, запуск 2: проверяю стабильность отпечатка и куку…")
        a2 = await run_session(exe, profA, pw, read_cookie=True)
        report["fingerprints"] = {"A (запуск 1)": a1, "A (запуск 2)": a2, "B": b1}

        # ---- шифрование и перенос
        log("Проверяю шифрование кук и перенос профиля…")
        tr: dict = {"cookie_value_set": cookie_val, "cookie_after_relaunch": a2.get("cookie")}
        try:
            oc = read_os_crypt(profA.data)
            tr["local_state_os_crypt_keys"] = sorted(oc.keys())
            tr["app_bound_key_present"] = "app_bound_encrypted_key" in oc
            tr["cookie_prefixes"] = cookie_prefixes(profA.data)
            raw = extract_key(profA.data)
            tr["key_extracted"] = len(raw) == 32
            (PROFILES / "A" / "os_crypt.key").write_bytes(raw)

            results = {}
            for variant in ("moved_nofix", "moved_fix"):
                dst = PROFILES / variant
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(profA.data, dst / "data",
                                ignore=shutil.ignore_patterns("Singleton*", "lockfile"))
                write_key(dst / "data", None)  # «другой компьютер»: ключ не расшифровывается
                before = key_decryptable(dst / "data")
                if variant == "moved_fix":
                    write_key(dst / "data", (PROFILES / "A" / "os_crypt.key").read_bytes())
                prof = Profile(variant, profA.seed, profA.cores, fwds["A"], profA.tz)
                prof.data = dst / "data"
                clear_locks(prof.data)
                r = await run_session(exe, prof, pw, collect=False, read_cookie=True)
                results[variant] = {"key_decryptable_before_fix": before,
                                    "key_decryptable_at_launch": key_decryptable(prof.data) if variant == "moved_fix" else before,
                                    "cookie": r.get("cookie"), "error": r.get("error")}
            tr["simulation"] = results
            tr["verdict"] = (
                "OK: восстановление ключа сохраняет куки при переносе"
                if results["moved_fix"]["cookie"] == cookie_val
                else "ПРОБЛЕМА: после восстановления ключа кука не прочиталась"
            )
        except Exception as e:  # noqa: BLE001
            tr["error"] = f"{type(e).__name__}: {e}"
        report["transfer"] = tr

    for f in fwds.values():
        await f.stop()
    report["finished"] = datetime.now().isoformat(timespec="seconds")
    write_report(report)
    log(f"Готово. Отчёт: {OUT / 'report.md'}")

    ans = input("\nОткрыть профиль A для ручной проверки на browserscan/creepjs/browserleaks? [y/N]: ")
    if ans.strip().lower() in ("y", "д", "yes", "да"):
        f = Forwarder(up1)
        await f.start()
        profA.fwd = f
        urls = ["https://www.browserscan.net/", "https://abrahamjuliot.github.io/creepjs/",
                "https://browserleaks.com/webrtc", "https://pixelscan.net/"]
        proc = await asyncio.create_subprocess_exec(str(exe), *profA.args(None), *urls)
        log("Профиль A открыт. Закрой окно браузера, чтобы завершить скрипт.")
        await proc.wait()
        await f.stop()


def write_report(report: dict) -> None:
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    real = report.get("real_ip")
    lines = [
        "# xlauncher — отчёт этапа 1",
        "",
        f"- Запуск: {report.get('started')} — {report.get('finished', 'не завершён')}",
        f"- Компьютер: {report.get('host')}, {report.get('os')}",
        f"- Движок: {(report.get('engine') or {}).get('version')}",
        f"- Реальный IP: {real}",
        "",
        "## Прокси",
        "",
    ]
    for k, p in (report.get("proxies") or {}).items():
        if p.get("ok"):
            g = p["geo"]
            lines.append(f"- {k}: {p['proxy']} → {p['ip']} {g.get('country')} {g.get('city')} "
                         f"{g.get('timezone')} {g.get('asn')} ({p['latency_s']} с)")
        else:
            lines.append(f"- {k}: {p['proxy']} → **АЛЯРМ**: {p.get('error')} {p.get('forwarder_errors')}")
    fps = report.get("fingerprints")
    if fps:
        lines += ["", "## Отпечатки", "", fp_rows(fps), ""]
        a1, a2, b = (fps.get("A (запуск 1)", {}).get("fp") or {}), (fps.get("A (запуск 2)", {}).get("fp") or {}), (fps.get("B", {}).get("fp") or {})
        checks = []
        for key in ("canvas", "audio", "webglRenderer"):
            checks.append(f"- {key}: A стабилен между запусками — {'да' if a1.get(key) == a2.get(key) else 'НЕТ'}; "
                          f"A отличается от B — {'да' if a1.get(key) != b.get(key) else 'НЕТ'}")
        leak = []
        for name, fp in (("A", a1), ("B", b)):
            ips = " ".join(fp.get("webrtc") or [])
            if not (report.get("proxies") or {}).get(name, {}).get("proxy"):
                leak.append(f"- {name}: без прокси — проверка утечек не применима")
            elif real and (real in ips or fp.get("ipSeen") == real):
                leak.append(f"- {name}: **УТЕЧКА реального IP**")
            else:
                leak.append(f"- {name}: реальный IP не виден (сайт видит {fp.get('ipSeen')})")
        crashes = [f"- {n}: {', '.join(v.get('crashed_probes') or []) or 'нет'}" for n, v in fps.items()]
        lines += ["## Выводы по отпечатку", "", *checks, "", "## Падения вкладок по пробам", "", *crashes, "",
                  "## Утечки", "", *leak, ""]
    tr = report.get("transfer")
    if tr:
        lines += ["## Перенос профиля", "", "```", json.dumps(tr, ensure_ascii=False, indent=2), "```", ""]
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nПрервано.")
