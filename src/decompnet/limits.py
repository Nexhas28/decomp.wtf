"""How a heavy tool that was stopped for using too much memory shows itself."""

from __future__ import annotations


def killed_for_memory(returncode: int) -> bool:
    return returncode in (137, -9)
