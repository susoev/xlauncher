# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright>=1.49"]
# ///
"""Диагностика падений canvas и подмены аудио в fingerprint-chromium.

Запуск из корня репозитория:  uv run spike\\canvas_diag.py

Без прокси (проверяется только поведение движка). Запускает браузер в нескольких
конфигурациях (разные seed, выключение отдельных подмен, без GPU) и в каждой
прогоняет набор маленьких тестов, каждый в своей вкладке. Результат:
spike\\out\\canvas_report.md и canvas_report.json.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stage1 import CACHE, OUT, PROFILES, ensure_engine, free_port, log, wait_cdp  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SEED1, SEED2 = 1111111111, 2222222222

CONFIGS: dict[str, list[str]] = {
    "seed1": [f"--fingerprint={SEED1}"],
    "seed2": [f"--fingerprint={SEED2}"],
    "seed1 без canvas-подмены": [f"--fingerprint={SEED1}", "--disable-spoofing=canvas"],
    "seed1 без font-подмены": [f"--fingerprint={SEED1}", "--disable-spoofing=font"],
    "seed1 без canvas+font": [f"--fingerprint={SEED1}", "--disable-spoofing=canvas,font"],
    "seed1 без всех подмен": [f"--fingerprint={SEED1}", "--disable-spoofing=font,audio,canvas,clientrects,gpu"],
    "seed1 без GPU-ускорения": [f"--fingerprint={SEED1}", "--disable-gpu"],
    "без --fingerprint": [],
}

# Маленький строковый хеш, чтобы не зависеть от crypto.subtle на about:blank.
H = "const h = s => { let x = 2166136261; for (let i = 0; i < s.length; i++) { x ^= s.charCodeAt(i); x = Math.imul(x, 16777619); } return (x >>> 0).toString(16); };"

TESTS: dict[str, str] = {
    "rect → toDataURL": f"""async () => {{ {H}
        const c = document.createElement('canvas'); c.width = 200; c.height = 50;
        const x = c.getContext('2d'); x.fillStyle = '#f60'; x.fillRect(10, 10, 100, 30);
        return h(c.toDataURL()); }}""",
    "measureText": """async () => {
        const x = document.createElement('canvas').getContext('2d'); x.font = '16px Arial';
        return x.measureText('Hello, world').width; }""",
    "fillText ascii (без выгрузки)": """async () => {
        const c = document.createElement('canvas'); c.width = 200; c.height = 50;
        const x = c.getContext('2d'); x.font = '16px Arial'; x.fillText('Hello, world', 5, 30);
        return 'ok'; }""",
    "fillText ascii → toDataURL": f"""async () => {{ {H}
        const c = document.createElement('canvas'); c.width = 200; c.height = 50;
        const x = c.getContext('2d'); x.font = '16px Arial'; x.fillText('Hello, world', 5, 30);
        return h(c.toDataURL()); }}""",
    "fillText unicode → toDataURL": f"""async () => {{ {H}
        const c = document.createElement('canvas'); c.width = 260; c.height = 50;
        const x = c.getContext('2d'); x.font = '16px Arial'; x.fillText('Привет \\u2713 \\ud83d\\ude03', 5, 30);
        return h(c.toDataURL()); }}""",
    "rect → getImageData": f"""async () => {{ {H}
        const c = document.createElement('canvas'); c.width = 50; c.height = 50;
        const x = c.getContext('2d'); x.fillStyle = '#069'; x.fillRect(0, 0, 50, 50);
        return h(Array.from(x.getImageData(0, 0, 50, 50).data).join(',')); }}""",
    "rect → toBlob": """async () => {
        const c = document.createElement('canvas'); c.width = 200; c.height = 50;
        const x = c.getContext('2d'); x.fillRect(10, 10, 100, 30);
        const b = await new Promise(r => c.toBlob(r)); return b.size; }""",
    "OffscreenCanvas → blob": """async () => {
        const c = new OffscreenCanvas(200, 50); const x = c.getContext('2d'); x.fillRect(10, 10, 100, 30);
        return (await c.convertToBlob()).size; }""",
    "WebGL readPixels": f"""async () => {{ {H}
        const gl = document.createElement('canvas').getContext('webgl');
        if (!gl) return 'нет webgl';
        gl.clearColor(0.2, 0.4, 0.6, 1); gl.clear(gl.COLOR_BUFFER_BIT);
        const p = new Uint8Array(4); gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, p);
        return Array.from(p).join(','); }}""",
    "audio: offline triangle": """async () => {
        const ctx = new OfflineAudioContext(1, 44100, 44100);
        const o = ctx.createOscillator(); o.type = 'triangle'; o.frequency.value = 10000;
        const comp = ctx.createDynamicsCompressor(); o.connect(comp); comp.connect(ctx.destination); o.start(0);
        const buf = await ctx.startRendering();
        let s = 0; for (const v of buf.getChannelData(0).slice(4500, 5000)) s += Math.abs(v);
        return s.toString(); }""",
    "audio: copyFromChannel": """async () => {
        const ctx = new OfflineAudioContext(1, 44100, 44100);
        const o = ctx.createOscillator(); o.type = 'sine'; o.frequency.value = 1000;
        o.connect(ctx.destination); o.start(0);
        const buf = await ctx.startRendering();
        const a = new Float32Array(1000); buf.copyFromChannel(a, 0, 4000);
        let s = 0; for (const v of a) s += Math.abs(v); return s.toString(); }""",
    "audio: analyser": """async () => {
        const ctx = new OfflineAudioContext(1, 44100, 44100);
        const o = ctx.createOscillator(); o.frequency.value = 1000;
        const an = ctx.createAnalyser(); o.connect(an); an.connect(ctx.destination); o.start(0);
        await ctx.startRendering();
        const d = new Float32Array(an.frequencyBinCount); an.getFloatFrequencyData(d);
        let s = 0; for (const v of d) if (isFinite(v)) s += v; return s.toString(); }""",
}


async def run_config(exe: Path, name: str, extra: list[str], pw) -> dict:
    slug = f"diag_{abs(hash(name)) % 10**8}"
    data = PROFILES / slug
    cache = CACHE / slug
    for p in (data, cache):
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    port = free_port()
    args = [f"--user-data-dir={data}", f"--disk-cache-dir={cache}", "--fingerprint-platform=windows",
            "--fingerprint-brand=Chrome", "--no-first-run", "--no-default-browser-check",
            f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1", *extra, "about:blank"]
    proc = await asyncio.create_subprocess_exec(str(exe), *args)
    res: dict = {}
    try:
        await wait_cdp(port)
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = browser.contexts[0]
        for tname, js in TESTS.items():
            pg = await ctx.new_page()
            try:
                await pg.goto("about:blank")
                v = await asyncio.wait_for(pg.evaluate(js), 20)
                res[tname] = {"ok": True, "value": v}
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                res[tname] = {"ok": False, "value": "ПАДЕНИЕ" if "crash" in msg.lower() else f"ошибка: {msg[:120]}"}
            finally:
                try:
                    await pg.close()
                except Exception:  # noqa: BLE001
                    pass
        cdp = await browser.new_browser_cdp_session()
        try:
            await cdp.send("Browser.close")
        except Exception:  # noqa: BLE001
            pass
        await asyncio.wait_for(proc.wait(), 20)
    except Exception as e:  # noqa: BLE001
        res["_error"] = f"{type(e).__name__}: {e}"
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    shutil.rmtree(data, ignore_errors=True)
    shutil.rmtree(cache, ignore_errors=True)
    return res


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    exe, ver = ensure_engine()
    from playwright.async_api import async_playwright

    results: dict[str, dict] = {}
    async with async_playwright() as pw:
        for name, extra in CONFIGS.items():
            log(f"Конфигурация: {name}")
            results[name] = await run_config(exe, name, extra, pw)
            crashed = [t for t, r in results[name].items() if not t.startswith("_") and not r["ok"]]
            log(f"  упало/ошибки: {', '.join(crashed) or 'нет'}")

    (OUT / "canvas_report.json").write_text(
        json.dumps({"engine": ver, "when": datetime.now().isoformat(timespec="seconds"), "results": results},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    cfgs = list(results)
    lines = [f"# Диагностика canvas/audio — движок {ver}", "",
             "| Тест | " + " | ".join(cfgs) + " |", "|---|" + "---|" * len(cfgs)]
    for t in TESTS:
        row = []
        for c in cfgs:
            r = results[c].get(t)
            row.append("—" if r is None else str(r["value"])[:24].replace("|", "\\|"))
        lines.append(f"| {t} | " + " | ".join(row) + " |")
    errs = {c: r["_error"] for c, r in results.items() if "_error" in r}
    if errs:
        lines += ["", "## Ошибки запуска", "", *[f"- {c}: {e}" for c, e in errs.items()]]
    (OUT / "canvas_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Готово. Отчёт: {OUT / 'canvas_report.md'}")


if __name__ == "__main__":
    asyncio.run(main())
