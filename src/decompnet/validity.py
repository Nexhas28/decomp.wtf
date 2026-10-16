"""Semantic validity guard: the byte-exact judge cannot see undefined behaviour.

A candidate that reads through an uninitialised pointer can reproduce the target's
instruction shapes -- and even reach score 0 -- while being a meaningless
reconstruction. Measured on slus-01363 ``func_800A4620``: a source whose only
assignment of the state-block pointer sat *after* a ``break;`` (dead code) scored 10
of 100 (99.9%) because gcc placed that undefined pseudo in ``$s1``; the emitted code
then dereferenced a state constant. Four goal rounds were spent trying to close its
last two rows before the undefined behaviour was spotted.

This module re-compiles a candidate with ``-Wall -Wuninitialized`` and reports what
the compiler says. The policy is deliberately asymmetric:

* an accepted **match** (score 0) is blocked when warnings appear -- a false match
  would enter the project as finished work;
* an ordinary **improvement** only carries a flag, so the guard can never reject a
  genuine result.

Measured false-positive rate: 0 of 14 randomly sampled matched functions raise
``-Wuninitialized`` on this project.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Sequence

#: Substrings gcc uses for an uninitialised use (2.8.1 through 13.x).
MARKERS: tuple[str, ...] = (
    "may be used uninitialized",
    "is used uninitialized",
    "used uninitialized",
)


@dataclass(frozen=True)
class Validity:
    """The compiler's verdict on one candidate source."""

    ok: bool
    warnings: tuple[str, ...] = ()
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "warnings": list(self.warnings), "detail": self.detail}


def warnings_in(log: str) -> tuple[str, ...]:
    """Extract uninitialised-use warnings from a compiler log."""
    hits = []
    for line in (log or "").splitlines():
        line = line.strip()
        if line and any(m in line for m in MARKERS):
            hits.append(line)
    return tuple(hits)


def check(compiler_cfg, header: str, source: str, compile_source) -> Validity:
    """Compile ``source`` with warnings enabled and report uninitialised uses.

    ``compile_source`` is passed in so the module stays import-light and testable
    without a toolchain; callers hand it :func:`decompnet.compiler.compile_source`.
    The probe never touches the content-addressed compile cache (``cache=False``),
    because it runs with different flags than the scored build.
    """
    if compiler_cfg is None:
        return Validity(ok=True, detail="no compiler configuration; guard skipped")
    cmd = list(getattr(compiler_cfg, "cmd", ()) or ())
    if "-Wuninitialized" not in cmd:
        cmd = cmd + ["-Wall", "-Wuninitialized"]
    try:
        probe = replace(compiler_cfg, cmd=cmd, cache=False)
    except TypeError:  # pragma: no cover - a config type without dataclass fields
        return Validity(ok=True, detail="compiler configuration cannot be probed")
    result = compile_source(probe, header, source)
    hits = warnings_in(getattr(result, "log", "") or "")
    if not hits:
        return Validity(ok=True)
    return Validity(ok=False, warnings=hits, detail=hits[0])


def describe(validity: Validity) -> str:
    """One-line, human-readable summary for notes and metadata."""
    if validity.ok:
        return "no uninitialised-use warnings"
    shown: Sequence[str] = validity.warnings[:3]
    return "uninitialised use: " + " | ".join(shown)
