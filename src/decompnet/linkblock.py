"""Functions that keep failing at the link step, not in the C.

A compile that reads right but can't be linked at the function's address ("`x` is not a known symbol", "your data
for D_80012340 differs from the ROM") is blocked by missing or wrong symbols and data, which no amount of rewriting
the C fixes. Agents still retry: the diff looks close, so they shuffle statements. This records the link problems
on the function's meta (``link_problems``) and, once several attempts by more than one agent all ended that way,
flags it ``link_blocked``: the claim brief then says to resolve the names first (read_data, rename_symbol,
edit_context), and the priority strategy hands the function out after everything else. A compile that links
clears both again.
"""
from __future__ import annotations

import re

ATTEMPTS = 3  #: link failures before a function counts as blocked ...
AGENTS = 2  #: ... when at least this many different agents hit them

_NAME = re.compile(r"`([^`]+)`|\b(D_[0-9A-Fa-f]{6,8}|func_[0-9A-Fa-f]{6,8}|jtbl_[0-9A-Fa-f]{6,8}|[A-Za-z_]\w*\+0x[0-9A-Fa-f]+)\b")


def names(problems: list[str]) -> list[str]:
    """The symbols and data the problems talk about, in order, without repeats."""
    out: list[str] = []
    for p in problems:
        for m in _NAME.finditer(p):
            name = m.group(1) or m.group(2)
            if name and name not in out:
                out.append(name)
    return out


def update(meta: dict, problems: list[str], agent_id: int) -> dict:
    """The function's meta after this attempt: problems recorded (or cleared when the attempt linked)."""
    meta = dict(meta)
    if not problems:
        meta.pop("link_problems", None)
        meta.pop("link_blocked", None)
        return meta
    rec = dict(meta.get("link_problems") or {})
    agents = list(rec.get("agents") or [])
    if agent_id not in agents:
        agents.append(agent_id)
    seen = list(rec.get("names") or [])
    for n in names(problems):
        if n not in seen:
            seen.append(n)
    rec = {"n": int(rec.get("n") or 0) + 1, "agents": agents[-8:], "names": seen[:12], "last": problems[:4]}
    meta["link_problems"] = rec
    if rec["n"] >= ATTEMPTS and len(agents) >= AGENTS:
        meta["link_blocked"] = True
    else:
        meta.pop("link_blocked", None)
    return meta


def blocked(meta: dict) -> bool:
    return bool(meta.get("link_blocked"))


def summary(meta: dict) -> str | None:
    """One line for the GUI, or None."""
    rec = meta.get("link_problems")
    if not rec or not blocked(meta):
        return None
    what = ", ".join(rec.get("names") or []) or "its data"
    return (f"{rec['n']} attempts by {len(rec.get('agents') or [])} agents all failed at linking, on {what}: "
            "the symbols or data need sorting out before the C can match.")


def brief_lines(meta: dict) -> list[str]:
    """The claim-brief block for a blocked function (empty when it isn't)."""
    rec = meta.get("link_problems")
    if not rec or not blocked(meta):
        return []
    lines = ["", f"### Needs symbols first ({rec['n']} attempts by {len(rec.get('agents') or [])} agents ended at the link step)",
             "The C isn't the problem: the names and data it refers to are. Before compiling again, read the data "
             "the assembly uses (read_data), give it its real name and size (rename_symbol, edit_context), and only "
             "then write the C against those names. The last link problems were:"]
    lines += [f"- {p}" for p in rec.get("last") or []]
    return lines
