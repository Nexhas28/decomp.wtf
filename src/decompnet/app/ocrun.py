"""An opencode helper session, for OpenCode 2: `python -m decompnet.app.ocrun <opencode> <run args...>`.

OpenCode 2's `opencode run` doesn't wait for MCP servers: it sends the prompt while decomp.wtf's tools are still
connecting, and the model then has none (it says the decomp tools aren't in its toolset). Its shared background
service is worse: it keeps the connection it made first, so the helper's own token is never used. So each session
gets its own `opencode serve`, waits until that server reports decomp connected, and `opencode run --server` sends
the prompt to it. The server goes when the run ends; stopping the helper ends both (one process group).

stdout carries opencode's JSON events (and our own error event, in the same shape, when decomp never connects).
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

WAIT = 90  # seconds for decomp.wtf's tools to connect (the first start of a new opencode build can be slow)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _error(message: str) -> None:
    print(json.dumps({"type": "error", "error": {"name": "decomp.wtf", "data": {"message": message}}}), flush=True)


def decomp_status(url: str, password: str, directory: str) -> tuple[str, str]:
    """(status, error) of the "decomp" MCP server on an opencode server, for this folder's config."""
    query = urllib.parse.urlencode({"location[directory]": directory})
    auth = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()
    req = urllib.request.Request(f"{url}/api/mcp?{query}", headers={"Authorization": auth})
    with urllib.request.urlopen(req, timeout=5) as r:
        servers = json.loads(r.read()).get("data") or []
    for s in servers:
        if s.get("name") == "decomp":
            st = s.get("status") or {}
            return str(st.get("status") or ""), str(st.get("error") or "")
    return "", ""


def main(argv: list[str]) -> int:
    exe, run_args = argv[0], argv[1:]
    password = secrets.token_urlsafe(18)
    env = dict(os.environ, OPENCODE_SERVER_PASSWORD=password)
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    server = subprocess.Popen([exe, "serve", "--hostname", "127.0.0.1", "--port", str(port)], env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        status, why, end = "", "", time.time() + WAIT
        while time.time() < end:
            if server.poll() is not None:
                _error(f"opencode's server stopped as it started (exit {server.returncode})")
                return 1
            try:
                status, why = decomp_status(url, password, os.getcwd())
            except (OSError, ValueError):
                status = ""  # not listening yet
            if status in ("connected", "failed", "needs_auth", "disabled"):
                break
            time.sleep(0.3)
        if status != "connected":
            _error("decomp.wtf's tools didn't connect in opencode"
                   + (f" ({status}{': ' + why if why else ''})" if status else f" within {WAIT} seconds"))
            return 1
        return subprocess.run([exe, "run", "--server", url, *run_args], env=env).returncode
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
