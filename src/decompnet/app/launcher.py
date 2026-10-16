"""Add decomp.wtf to ~/Applications: a small app bundle that just runs `decomp`, which opens the app in the
browser."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _decomp_exe() -> str:
    found = shutil.which("decomp")
    if found:
        return found
    exe = Path(sys.argv[0]).resolve()
    return str(exe)


def install_launcher() -> str:
    exe = _decomp_exe()
    app = Path.home() / "Applications" / "decomp.wtf.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
        "<key>CFBundleName</key><string>decomp.wtf</string>"
        "<key>CFBundleIdentifier</key><string>wtf.decomp.app</string>"
        "<key>CFBundleExecutable</key><string>decomp-wtf</string>"
        "<key>CFBundlePackageType</key><string>APPL</string>"
        "<key>LSUIElement</key><true/></dict></plist>\n", encoding="utf-8")
    script = macos / "decomp-wtf"
    script.write_text(f'#!/bin/sh\nexec "{exe}" >/dev/null 2>&1\n', encoding="utf-8")
    os.chmod(script, 0o755)
    return f"added decomp.wtf to {app.parent}"
