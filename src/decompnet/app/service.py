"""Run decomp.wtf in the background, starting with the computer: for a Mac that hosts a team's room.

A launchd agent of this user runs `decomp --no-browser`, nothing more; `decomp service remove` takes it away again.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "wtf.decomp"


def _command() -> list[str]:
    exe = shutil.which("decomp")
    return [exe, "--no-browser"] if exe else [sys.executable, "-m", "decompnet.app.cli", "--no-browser"]


def _plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install() -> str:
    cmd = _command()
    home = os.environ.get("DECOMP_HOME")
    plist = _plist()
    plist.parent.mkdir(parents=True, exist_ok=True)
    args = "".join(f"<string>{a}</string>" for a in cmd)
    env = f"<key>EnvironmentVariables</key><dict><key>DECOMP_HOME</key><string>{home}</string></dict>" if home else ""
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array>{args}</array>{env}
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
""", encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    res = subprocess.run(["launchctl", "load", str(plist)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "launchctl failed")
    return f"decomp.wtf runs in the background now (launchd agent {plist}); it starts when you log in."


def remove() -> str:
    subprocess.run(["launchctl", "unload", str(_plist())], capture_output=True)
    _plist().unlink(missing_ok=True)
    return "decomp.wtf no longer runs as a service."


def installed() -> bool:
    return _plist().exists()
