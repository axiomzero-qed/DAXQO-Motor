"""Exercise an installed Motor from the official independent MCP client.

No DAXQO server, Collector, model or physical phone is used. The child process
runs from a temporary directory, so a sibling development checkout cannot help.
"""
import argparse
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check(python):
    with tempfile.TemporaryDirectory(prefix="motor-external-host-") as temporary:
        params = StdioServerParameters(command=python, args=[
            "-m", "motor.mcp_server", "--adapter", "motor.demo:create_adapter",
            "--map-db", str(Path(temporary) / "map.sqlite3"), "--source", "independent_mcp_host"],
            cwd=temporary, env={"PYTHONUTF8": "1"})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=20)) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                expected = {"motor_map", "motor_scout", "motor_navigate", "motor_reflex", "motor_cancel"}
                assert expected == names, names
                async def call(name, arguments):
                    result = await session.call_tool(name, arguments)
                    assert not result.isError, result
                    return json.loads(result.content[0].text)
                scout = await call("motor_scout", {"package": "demo.app", "seconds": 4, "max_scrolls": 1, "max_depth": 0})
                assert scout["ok"], scout
                await call("motor_map", {"package": "demo.app"})
                clicked = await call("motor_reflex", {"package": "demo.app", "action": {
                    "kind": "CLICK", "selector": {"text": "Details"}}})
                assert clicked["ok"] and clicked["status"] == "verified", clicked
                assert clicked["current"]["title"] == "Demo details", clicked
                returned = await call("motor_reflex", {"package": "demo.app", "action": {"kind": "BACK"}})
                assert returned["ok"] and returned["current"]["title"] == "Demo home", returned
                await call("motor_cancel", {"reason": "host_smoke_completed"})
                print(json.dumps({"host": "official MCP Python SDK", "server": initialized.serverInfo.name,
                    "protocol": initialized.protocolVersion, "backend": "synthetic", "tools": sorted(names),
                    "observed_click_and_back": True, "collector_required": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Python containing the installed server package")
    asyncio.run(check(parser.parse_args().python))
