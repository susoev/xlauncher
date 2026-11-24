"""Проба отпечатка: что движок на самом деле отдаёт сайтам для данного seed (ARCHITECTURE.md §4.3, §4.6).

Подмена audio в движке даёт мало вариантов, поэтому разные seed иногда совпадают между собой
или с браузером без подмены. Проба запускает движок на пару секунд во временной папке, за
пределами экрана, без сети, и снимает audio, canvas и WebGL через CDP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("xlauncher.probe")

PROBE_JS = r"""(async () => {
  const h = s => { let x = 2166136261; for (let i = 0; i < s.length; i++) { x ^= s.charCodeAt(i); x = Math.imul(x, 16777619); } return (x >>> 0).toString(16); };
  const r = {};
  try {
    const ctx = new OfflineAudioContext(1, 44100, 44100);
    const o = ctx.createOscillator(); o.type = 'triangle'; o.frequency.value = 10000;
    const comp = ctx.createDynamicsCompressor(); o.connect(comp); comp.connect(ctx.destination); o.start(0);
    const buf = await ctx.startRendering();
    let s = 0; for (const v of buf.getChannelData(0).slice(4500, 5000)) s += Math.abs(v);
    r.audio = s.toString();
  } catch (e) { r.audio = 'err'; }
  try {
    const c = document.createElement('canvas'); c.width = 64; c.height = 32;
    const x = c.getContext('2d'); x.fillStyle = '#f60'; x.fillRect(4, 4, 40, 20); x.fillStyle = '#069'; x.fillRect(20, 10, 30, 15);
    r.canvas = h(Array.from(x.getImageData(0, 0, 64, 32).data).join(','));
  } catch (e) { r.canvas = 'err'; }
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    r.webgl = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
  } catch (e) { r.webgl = 'err'; }
  return r;
})()"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get_json(url: str, timeout: float = 2) -> object:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return json.loads(r.read())


async def _cdp_eval(ws_url: str, expression: str, timeout: float = 20) -> dict:
    import websockets

    async with websockets.connect(ws_url, max_size=None, proxy=None) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expression, "awaitPromise": True, "returnByValue": True}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), deadline - time.monotonic()))
            if msg.get("id") == 1:
                if "exceptionDetails" in msg.get("result", {}):
                    raise RuntimeError(msg["result"]["exceptionDetails"].get("text", "ошибка JS"))
                return msg["result"]["result"]["value"]
    raise TimeoutError("CDP: нет ответа")


async def _close(ws_url: str) -> None:
    import websockets

    try:
        async with websockets.connect(ws_url, proxy=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            await asyncio.wait_for(ws.recv(), 3)
    except Exception:  # noqa: BLE001
        pass


def probe(exe: Path, seed: int | None, hardware_concurrency: int = 8, timeout: float = 40) -> dict:
    """Запускает движок с seed (None — без подмены) и возвращает {audio, canvas, webgl}."""
    tmp = Path(tempfile.mkdtemp(prefix="xl-probe-"))
    port = _free_port()
    args = [str(exe), f"--user-data-dir={tmp}", f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
            "--window-position=-32000,-32000", "--window-size=400,300",
            "--proxy-server=http://127.0.0.1:9", "--disable-background-networking", "about:blank"]
    args[1:1] = os.environ.get("XL_PROBE_EXTRA_ARGS", "").split()  # для тестов: --no-sandbox --headless=new
    if seed is not None:
        args[1:1] = [f"--fingerprint={seed}", "--fingerprint-platform=windows", "--fingerprint-brand=Chrome",
                     f"--fingerprint-hardware-concurrency={hardware_concurrency}"]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    try:
        t0 = time.monotonic()
        page_ws = None
        while time.monotonic() - t0 < timeout:
            if proc.poll() is not None:
                raise RuntimeError(f"движок завершился при пробе (код {proc.returncode})")
            try:
                targets = _get_json(f"http://127.0.0.1:{port}/json/list")
                page_ws = next((t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page"), None)
                if page_ws:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.3)
        if not page_ws:
            raise TimeoutError("движок не открыл порт отладки")
        result = asyncio.run(_cdp_eval(page_ws, PROBE_JS))
        try:
            browser_ws = _get_json(f"http://127.0.0.1:{port}/json/version")["webSocketDebuggerUrl"]
            asyncio.run(_close(browser_ws))
        except Exception:  # noqa: BLE001
            pass
        return result
    finally:
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)
        shutil.rmtree(tmp, ignore_errors=True)
