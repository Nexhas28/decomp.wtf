"""Where decomp.wtf runs, and how it reaches the computer around it.

decomp.wtf runs on the user's own Mac, so opening the browser or a folder and reading a path the user typed
(~/Downloads/...) need nothing in between.
"""

from __future__ import annotations

import subprocess
import webbrowser
from pathlib import Path


def runtime() -> str:
    """Always "native": decomp.wtf runs on the Mac itself (the app reads it from /api/status)."""
    return "native"


def host_os() -> str:
    """The system the user sits at."""
    return "macos"


def local_path(text: str) -> Path:
    """A file path the user (or their agent) gave, as decomp.wtf sees it."""
    return Path(text.strip().strip('"').strip("'")).expanduser()


def shown_path(path: Path | str) -> str:
    """A path of decomp.wtf's, as the user can open it."""
    return str(path)


def open_url(url: str) -> bool:
    """Open a page in the user's browser."""
    try:
        return webbrowser.open(url)
    except Exception:  # no browser at all
        return False


def open_folder(path: Path) -> str | None:
    """Show a folder in Finder. Returns why it couldn't, or None."""
    subprocess.Popen(["open", str(path)])
    return None


def info() -> dict:
    return {"runtime": runtime(), "host_os": host_os()}
