"""Per-function compiler comments in a submitted source.

    /* decomp-compiler: gcc-2.7.2-psx */      another registry compiler for the same platform
    /* decomp-assembler: maspsx-2.79 */       how the compiler's assembly is assembled
    /* decomp-cflags: -fno-schedule-insns */  extra compiler flags, from an allow-list

A room's host and this copy both apply the ones listed here, so a stored best reproduces on either side. Anything
else (an assembler profile this copy doesn't define, a flag outside the allow-list) is `unknown`: only a host that
defines it can judge that source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_COMMENT = re.compile(r"/\*\s*decomp-(compiler|assembler|cflags)\s*:\s*(.*?)\s*\*/", re.S)

#: Assembler profiles. "gnu-old": the toolchain's own (period) assembler, the default pipeline. "gnu-modern": the
#: modern MIPS binutils with the PSY-Q passes. "maspsx-…": gcc -S, then maspsx emulating that ASPSX version, then
#: the modern binutils (the recipe the PlayStation decomps use).
ASSEMBLERS: dict[str, tuple[str, ...] | None] = {
    "gnu-old": None,
    "gnu-modern": None,
    "maspsx-2.79": ("--aspsx-version=2.79",),
    "maspsx-2.79-div": ("--aspsx-version=2.79", "--expand-div"),
}

#: GCC options a source may ask for: optimisation switches (each also as -fno-…) and MIPS address splitting.
#: Nothing that names a file, a path or a plugin: the source is untrusted.
_F_OPTIONS = frozenset({
    "schedule-insns", "schedule-insns2", "force-addr", "force-mem", "peephole", "cse-follow-jumps",
    "cse-skip-blocks", "rerun-cse-after-loop", "rerun-loop-opt", "expensive-optimizations", "thread-jumps",
    "caller-saves", "strength-reduce", "delayed-branch", "omit-frame-pointer", "inline-functions",
    "defer-pop", "gcse", "regmove", "unroll-loops", "loop-optimize", "common", "signed-char", "unsigned-char",
    "keep-static-consts", "strict-aliasing", "optimize-sibling-calls",
})
_M_OPTIONS = frozenset({"-msplit-addresses", "-mno-split-addresses", "-mgpopt", "-mno-gpopt"})
_FLAG = re.compile(r"^-f(?:no-)?([a-z0-9-]+)$")


def flag_allowed(flag: str) -> bool:
    if flag in _M_OPTIONS:
        return True
    m = _FLAG.match(flag)
    return bool(m and m.group(1) in _F_OPTIONS)


@dataclass(frozen=True)
class Directives:
    compiler: str | None = None
    assembler: str | None = None
    cflags: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()  # comments this copy can't apply, as written

    @property
    def applied(self) -> bool:
        """Whether this copy builds the source differently because of its comments."""
        return bool(self.compiler or self.assembler or self.cflags)

    @property
    def any(self) -> bool:
        return bool(self.compiler or self.assembler or self.cflags or self.unknown)


def parse(source: str, compilers: frozenset[str] | set[str] | None = None) -> Directives:
    """The comments in `source`. `compilers`: the compiler ids allowed here (None: any registry id); the last
    comment of each kind wins, cflags accumulate."""
    compiler = assembler = None
    cflags: list[str] = []
    unknown: list[str] = []
    for kind, value in _COMMENT.findall(source or ""):
        value = " ".join(value.split())
        if kind == "compiler":
            if value and (compilers is None or value in compilers):
                compiler = value
            else:
                unknown.append(f"decomp-compiler: {value}")
        elif kind == "assembler":
            if value in ASSEMBLERS:
                assembler = value
            else:
                unknown.append(f"decomp-assembler: {value}")
        else:
            for flag in value.split():
                if flag_allowed(flag):
                    if flag not in cflags:
                        cflags.append(flag)
                else:
                    unknown.append(f"decomp-cflags: {flag}")
    return Directives(compiler, assembler, tuple(cflags), tuple(unknown))
