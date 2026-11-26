"""Брандмауэр Windows для удалённого доступа (MCP по HTTP).

Лаунчер каждой версии лежит в своей папке (app/<версия>/python/XLauncher.exe), поэтому Windows
при первом запуске новой версии снова спрашивает про доступ к сети. Если окно закрыть или нажать
«Отмена», появляются запрещающие правила для этого exe — а запрет сильнее любого разрешения,
и удалённый доступ молча перестаёт работать.

Решение — одно правило по порту, не зависящее от пути: TCP <порт> только с адресов Tailscale,
и снятие запретов для exe лаунчера. Для этого нужны права администратора (UAC один раз).

check() — без прав: {"supported", "rule", "rule_ports", "blocks", "ok", "error"}
fix(port) — с запросом UAC: снимает запреты для exe из папки лаунчера, ставит правило.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("xlauncher.firewall")

RULE_NAME = "XLauncher MCP (Tailscale)"
TAILNET = ("100.64.0.0/10", "fd7a:115c:a1e0::/48")
TIMEOUT_S = 30
FIX_WAIT_S = 120

_cache: dict = {}
_lock = threading.Lock()


def supported() -> bool:
    return sys.platform == "win32"


def exe_path() -> Path:
    return Path(sys.executable)


def app_root() -> Path:
    """Папка со всеми версиями (app/): запреты для exe любой версии отсюда — наши."""
    exe = exe_path()
    # app/<версия>/python/XLauncher.exe → app
    return exe.parents[2] if len(exe.parents) > 2 and exe.parent.name.lower() == "python" else exe.parent


def _q(s: str) -> str:
    """Строка в одинарных кавычках PowerShell."""
    return "'" + str(s).replace("'", "''") + "'"


def check_script(exe: Path) -> str:
    return f"""$ErrorActionPreference = 'SilentlyContinue'
$exe = {_q(exe)}
$b = @(Get-NetFirewallApplicationFilter | Where-Object {{ $_.Program -eq $exe }} | Get-NetFirewallRule |
  Where-Object {{ $_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' }}).Count
$r = @(Get-NetFirewallRule -DisplayName {_q(RULE_NAME)} | Where-Object {{ $_.Enabled -eq 'True' -and $_.Action -eq 'Allow' }})
$ports = @($r | Get-NetFirewallPortFilter | ForEach-Object {{ [string]$_.LocalPort }})
@{{ blocks = $b; rule = $r.Count; ports = $ports }} | ConvertTo-Json -Compress
"""


def fix_script(root: Path, port: int, result: Path) -> str:
    remote = ",".join(_q(n) for n in TAILNET)
    return f"""$ErrorActionPreference = 'Stop'
try {{
  $root = {_q(root)}
  $blocks = @(Get-NetFirewallApplicationFilter |
    Where-Object {{ $_.Program -and $_.Program.StartsWith($root + '\\', [StringComparison]::OrdinalIgnoreCase) }} |
    Get-NetFirewallRule | Where-Object {{ $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' }})
  $n = $blocks.Count
  if ($n) {{ $blocks | Remove-NetFirewallRule }}
  Get-NetFirewallRule -DisplayName {_q(RULE_NAME)} -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  New-NetFirewallRule -DisplayName {_q(RULE_NAME)} -Description 'Удалённый доступ агентов к XLauncher только из сети Tailscale' `
    -Direction Inbound -Protocol TCP -LocalPort {int(port)} -RemoteAddress {remote} -Action Allow -Profile Any | Out-Null
  Set-Content -LiteralPath {_q(result)} -Encoding UTF8 -Value ('ok ' + $n)
}} catch {{
  Set-Content -LiteralPath {_q(result)} -Encoding UTF8 -Value ('error ' + $_.Exception.Message)
}}
"""


def _powershell(script: str, timeout: float = TIMEOUT_S) -> str:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    enc = base64.b64encode(script.encode("utf-16-le")).decode()  # без возни с кавычками в командной строке
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-EncodedCommand", enc], capture_output=True, timeout=timeout, creationflags=flags)
    return r.stdout.decode("utf-8", "replace").strip()


def evaluate(raw: dict, port: int) -> dict:
    ports = raw.get("ports") or []
    ports = [str(p) for p in ([ports] if isinstance(ports, (str, int)) else ports)]
    rule = bool(raw.get("rule")) and (str(port) in ports or "Any" in ports)
    blocks = int(raw.get("blocks") or 0)
    return {"supported": True, "rule": rule, "rule_ports": ports, "blocks": blocks, "ok": rule and not blocks}


def check(port: int) -> dict:
    if not supported():
        return {"supported": False, "ok": True}
    try:
        out = _powershell(check_script(exe_path()))
        res = evaluate(json.loads(out or "{}"), port)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        res = {"supported": True, "ok": None, "error": f"не удалось проверить брандмауэр: {e}"[:300]}
    with _lock:
        _cache.clear()
        _cache.update(res, port=port, checked=time.time())
    return res


def current() -> dict:
    with _lock:
        return dict(_cache)


def check_async(port: int) -> None:
    if supported():
        threading.Thread(target=check, args=(port,), name="firewall-check", daemon=True).start()


def _run_elevated(script_file: Path) -> int:
    """powershell -File <script> от администратора; код ShellExecute (>32 — запущено, 5 — UAC отклонён)."""
    import ctypes

    args = f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{script_file}"'
    return int(ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", args, None, 0))


def fix(port: int) -> dict:
    try:
        return _fix(port)
    except Exception as e:  # noqa: BLE001
        log.exception("брандмауэр: исправление")
        return {"ok": False, "error": f"не удалось настроить брандмауэр: {type(e).__name__}: {e}"[:300],
                "firewall": current()}


def _read_result(result: Path) -> str:
    """Файл результата пишет PowerShell от администратора: пока он открыт, Windows не даёт его читать."""
    try:
        return result.read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError:
        return ""


def _fix(port: int) -> dict:
    if not supported():
        return {"ok": True, "firewall": {"supported": False, "ok": True}}
    tmp = Path(tempfile.mkdtemp(prefix="xl-fw-"))
    result = tmp / "result.txt"
    script = tmp / "fix.ps1"
    # UTF-8 с BOM: Windows PowerShell 5 без BOM читает файл в ANSI и ломает кириллицу в путях
    script.write_text(fix_script(app_root(), port, result), encoding="utf-8-sig")
    code = _run_elevated(script)
    if code <= 32:
        return {"ok": False, "error": "Windows не дала прав администратора" if code == 5
                else f"не удалось запустить PowerShell (код {code})", "firewall": check(port)}
    deadline = time.time() + FIX_WAIT_S
    text = ""
    while time.time() < deadline:
        if result.exists():
            time.sleep(0.3)
            text = _read_result(result)
            if text:
                break
        time.sleep(0.5)
    fw = check(port)
    if text.startswith("ok"):
        log.info("брандмауэр: правило по порту %s, снято запретов: %s", port, text[3:])
        n = text[3:].strip()
        return {"ok": True, "removed_blocks": int(n) if n.isdigit() else 0, "firewall": fw}
    err = text[6:] if text.startswith("error") else (
        "окно PowerShell от администратора не ответило за 2 минуты — возможно, запрос прав был отклонён")
    log.warning("брандмауэр: %s", err)
    return {"ok": False, "error": err, "firewall": fw}


def selftest_fix(port: int = 47111) -> dict:
    """Только для CI (раннер уже администратор): скрипт исправления без UAC, затем проверка."""
    tmp = Path(tempfile.mkdtemp(prefix="xl-fw-"))
    result = tmp / "result.txt"
    _powershell(fix_script(app_root(), port, result), timeout=120)
    return {"result": _read_result(result), "check": check(port)}
