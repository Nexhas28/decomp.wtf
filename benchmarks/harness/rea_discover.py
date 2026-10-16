#!/usr/bin/env python3
"""One-shot discovery driver: MCP initialize + tools/list against the pinned rea-agents install.

Read-only: sends JSON-RPC over stdio, dumps the tool catalog to a file, exits.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REA_ROOT = Path("/Users/shanemcgovern/.local/lib/node_modules/rea-agents")
OUT = Path("/tmp/rea_tools_list.json")


def send(proc, payload):
    line = json.dumps(payload)
    proc.stdin.write(line.encode() + b"\n")
    proc.stdin.flush()


def read_message(proc):
    line = proc.stdout.readline()
    if not line:
        return None
    return json.loads(line)


def main():
    env = {
        "HOME": tempfile.mkdtemp(prefix="rea-discovery-home-"),
        "TMPDIR": tempfile.mkdtemp(prefix="rea-discovery-tmp-"),
        "PATH": "/Users/shanemcgovern/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C",
    }
    cmd = ["/Users/shanemcgovern/.local/bin/node", "scripts/rea.mjs", "mcp"]
    proc = subprocess.Popen(cmd, cwd=REA_ROOT, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18",
                               "capabilities": {},
                               "clientInfo": {"name": "rea-schema-discovery", "version": "0.0.1"}}})
        init_resp = read_message(proc)
        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = None
        while True:
            msg = read_message(proc)
            if msg is None:
                break
            if msg.get("id") == 2:
                tools = msg
                break
        send(proc, {"jsonrpc": "2.0", "id": 3, "method": "shutdown"})
        OUT.write_text(json.dumps({"initialize": init_resp, "tools": tools}, indent=1))
        names = sorted(t.get("name") for t in (tools or {}).get("result", {}).get("tools", []))
        print(f"protocolVersion: {(init_resp or {}).get('result', {}).get('protocolVersion')}")
        print(f"serverInfo: {(init_resp or {}).get('result', {}).get('serverInfo')}")
        print(f"tool count: {len(names)}")
        print("\n".join(names))
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read().decode(errors="replace")
        if err.strip():
            print(f"stderr tail: {err.strip()[-2000:]}", file=sys.stderr)


if __name__ == "__main__":
    main()
