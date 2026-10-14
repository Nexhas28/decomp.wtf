"""`decomp mcp`: decomp.wtf for the agent you talk to.

Add it once to Claude Code, Codex, Gemini CLI or opencode (`claude mcp add decomp -- decomp mcp`, ...). When that
agent uses it, this starts decomp.wtf in the background if it isn't running, gets the agent its own identity, and
passes the app's tools through over stdio: the agent can import your game, work on it, start helper agents and open
the live view, and everything it does shows up in the app as it happens.

stdout is the MCP channel: everything else goes to stderr.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .. import __version__
from .cli import _alive, home_dir, replace_older


def _log(msg: str) -> None:
    print(f"decomp mcp: {msg}", file=sys.stderr, flush=True)


def _running(state: Path) -> dict | None:
    try:
        info = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if info.get("port") and _alive(info["port"]) else None


def ensure_app(home: Path) -> dict:
    """The running app's {port, secret}, starting it in the background first if needed."""
    internal = home / ".decomp"
    state = internal / "server.json"
    if replace_older(state, log=_log) and (info := _running(state)):  # an older version is restarted as this one
        return info
    internal.mkdir(parents=True, exist_ok=True)
    _log("starting decomp.wtf in the background")
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS} \
        if sys.platform == "win32" else {"start_new_session": True}
    with open(internal / "app.log", "ab") as out:
        subprocess.Popen([sys.executable, "-m", "decompnet.app.cli", "--no-browser"], stdin=subprocess.DEVNULL,
                         stdout=out, stderr=out, **kwargs)
    for _ in range(300):
        time.sleep(0.2)
        if (info := _running(state)):
            return info
    raise SystemExit(f"decomp mcp: decomp.wtf didn't start; see {internal / 'app.log'}")


def mint(info: dict, name: str) -> str:
    port = info["port"]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/agents/token", method="POST",
                                 data=json.dumps({"name": name}).encode(),
                                 headers={"Cookie": f"decomp_live={info['secret']}", "X-Decomp": "1",
                                          "Host": f"127.0.0.1:{port}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    _log(f"connected as {data['agent']}")
    return data["token"]


async def proxy(port: int, token: str) -> None:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.lowlevel.server import Server
    from mcp.server.stdio import stdio_server

    from .chat import CHAT_INSTRUCTIONS

    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(900, connect=10))
    async with Client(streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http),
                      read_timeout_seconds=900) as remote:
        async def list_tools(ctx, params):
            return await remote.list_tools(cursor=params.cursor if params else None)

        async def call_tool(ctx, params):
            return await remote.call_tool(params.name, params.arguments or {})

        server = Server("decomp", version=__version__, title="decomp.wtf",
                        instructions=CHAT_INSTRUCTIONS + "\n" + (remote.instructions or ""),
                        on_list_tools=list_tools, on_call_tool=call_tool)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


def main(name: str = "chat") -> int:
    try:
        info = ensure_app(home_dir())
        token = mint(info, name)
    except (OSError, ValueError, KeyError) as e:
        _log(f"couldn't connect to decomp.wtf: {e}. Try `decomp stop`, then use this again.")
        return 1
    try:
        asyncio.run(proxy(info["port"], token))
    except KeyboardInterrupt:
        pass
    return 0
