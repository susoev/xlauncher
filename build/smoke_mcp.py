"""CI: MCP-рукопожатие со встроенным Python пакета на Windows.

    python\\python.exe ..\\build\\smoke_mcp.py
"""

import asyncio
import os
import sys

import xlauncher  # noqa: F401  — как при реальном запуске: пакет подключает lib\\ (pywin32)
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "xlauncher.mcp_server"], env=dict(os.environ))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        need = {"list_profiles", "start_profile", "browser_snapshot", "browser_click", "browser_screenshot"}
        assert need <= names, names
        res = await s.call_tool("list_profiles", {})
        print("MCP OK:", len(names), "инструментов;", res.content[0].text[:80] if res.content else "")


asyncio.run(main())
