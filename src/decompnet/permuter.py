"""Donated compute, volunteer side: `decompnet worker`.

Fetches a near-matching function from the coordinator, rewrites the best known C source, compiles every
variant locally with the same pinned toolchain, scores it with the same scorer, and submits improvements.
The coordinator re-verifies everything it receives.

Mutations (plan P5.1-P5.3) are syntax-aware and labelled, in two categories:

* ``restricted``  - an equivalence rewrite that is applied only after its dependency/type preconditions
  hold (no volatile accesses, unknown calls, pointer/array/member accesses, overlapping writes, ...).
  These are equivalence claims under *explicit assumptions*: the notes record the assumptions, and the
  checks never promise full C semantic equivalence (aliasing through globals, undefined behaviour,
  evaluation order and compiler extensions can still invalidate naive rules).
* ``exploratory`` - deliberately tests a different type/expression/control-flow interpretation because
  the current draft may be wrong. Always labelled; the authoritative byte judge decides, never an
  equivalence claim.

The source is handled by a lightweight token/line-based analyser (no parser dependency; plan P5.2):
untouched text is preserved byte for byte, mutation locations carry source spans, and constructs the
analyser cannot own (inline assembly, preprocessor directives inside the body, multi-statement lines,
compiler extensions, ...) are reported in ``Analysis.unsupported`` instead of being silently rewritten.
Sources the analyser cannot structure at all (no recognisable function body) fall back to the legacy
text engine, which is explicitly labelled ``fallback`` and never claims equivalence.

Every mutation carries provenance (parent digest, operator id, category, spans) so search runs can be
replayed (P5.5/P5.6 groundwork; ``mutate(..., record=...)`` collects the records).

Runs only GPL-licensed toolchains automatically; proprietary ones are used only if you installed them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import scoring, toolchains
from .compiler import compile_source
from .elfobj import FunctionObj, Reloc, extract_function
from .project import compiler_config

# ------------------------------------------------------------- categories (P5.1)

RESTRICTED = "restricted"    # equivalence rewrite, applied only after checked assumptions
EXPLORATORY = "exploratory"  # deliberately tests a different interpretation; the judge decides
FALLBACK = "fallback"        # legacy text engine for sources the analyser cannot structure

TYPES_32 = ["int", "unsigned", "unsigned int", "long", "unsigned long", "s32", "u32", "uInt", "uLong"]
CASTS = ["(int)", "(unsigned)", "(s32)", "(u32)", "(uInt)", "(uLong)", "(short)", "(unsigned short)"]

# ----------------------------------------------------------- provenance (P5.2/5.5)


@dataclass(frozen=True)
class Span:
    """A half-open source region: lines are 0-based; end_col is exclusive."""

    start_line: int
    start_col: int
    end_line: int
    end_col: int

    @staticmethod
    def single(line: int, start: int, end: int) -> "Span":
        return Span(line, start, line, end)

    @staticmethod
    def whole(lines: list[str], first: int, last: int) -> "Span":
        return Span(first, 0, last, len(lines[last]))


@dataclass(frozen=True)
class Unsupported:
    span: Span
    reason: str


@dataclass(frozen=True)
class Mutation:
    """One applied mutation with its provenance: parent digest, operator, category and spans."""

    operator: str
    category: str
    parent_digest: str
    child_digest: str
    spans: tuple[Span, ...]
    text: str
    note: str = ""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Statement:
    """One body line, classified by the lightweight analyser (P5.2)."""

    line: int
    kind: str  # blank/decl/assign/compound/incdec/expr/control/block/label/unsupported
    indent: int
    depth: int
    scope: int
    text: str = ""                      # code view: strings/comments blanked, length preserved
    writes: frozenset[str] = frozenset()
    reads: frozenset[str] = frozenset()
    calls: bool = False
    memory: bool = False
    volatile: bool = False
    kw: str = ""                        # control keyword, or "register" on a register declaration
    # declaration detail
    name: str = ""
    type_text: str = ""
    type_span: tuple[int, int] | None = None   # the type-word region (for signedness exploration)
    qual_span: tuple[int, int] | None = None   # the qualifier region (for storage hints)
    pointer: bool = False
    array: bool = False
    vla: bool = False
    has_init: bool = False
    reorderable: bool = False


@dataclass
class Analysis:
    source: str
    lines: list[str]
    body: tuple[int, int] | None        # half-open statement range, or None (not structurable)
    stmts: dict[int, Statement]
    decls: dict[str, Statement]
    params: dict[str, tuple[str, bool, bool]]  # name -> (type text, pointer, array)
    volatile_names: frozenset[str]
    address_taken: frozenset[str]
    unsupported: list[Unsupported]

    @property
    def text(self) -> str:
        """The round trip of the analysed source: byte-identical to the input (P-01)."""
        return "\n".join(self.lines)


@dataclass(frozen=True)
class Operator:
    id: str
    category: str
    note: str
    apply: Callable[["Analysis", "random.Random"], tuple[list[str], tuple[Span, ...]] | None]


OPERATORS: dict[str, Operator] = {}


def _op(op_id: str, category: str, note: str):
    def deco(fn):
        OPERATORS[op_id] = Operator(op_id, category, note, fn)
        return fn
    return deco


# ------------------------------------------------------- lightweight analyser (P5.2)

_TYPE_KEYWORDS = frozenset(
    "char short int long float double void signed unsigned struct union enum const volatile register "
    "static extern auto size_t ssize_t bool s8 u8 s16 u16 s32 u32 s64 u64 uInt uLong sint uint "
    "int8_t uint8_t int16_t uint16_t int32_t uint32_t int64_t uint64_t byte ushort ulong "
    "u_char u_short u_int u_long undefined UNK_TYPE UNK_PTR".split()
) | frozenset(TYPES_32)
_FLOAT_WORDS = ("float", "double", "struct", "union", "enum")

_CONTROL_KW = re.compile(r"(if|else|for|while|do|switch|return|break|continue|goto|case|default)\b")
_EXTENSIONS = re.compile(r"\b(__attribute__|__typeof__|typeof|_Pragma|__extension__|__asm__|asm)\b")
_LABEL = re.compile(r"^[ \t]*[A-Za-z_]\w*[ \t]*:")
_DECL_STMT = re.compile(
    r"^(\s*)"                                                    # 1 indent
    r"((?:(?:register|auto|static|volatile|const|extern)\s+)*)"  # 2 qualifiers
    r"(?:(unsigned|signed)\s+)?"                                 # 3
    r"([A-Za-z_]\w*)"                                            # 4 first type word
    r"((?:\s+[A-Za-z_]\w*)*?)"                                   # 5 further type words (reluctant: the
                                                                 #   declarator name is the last identifier)
    r"\s*(\**)\s*"                                               # 6 stars
    r"([A-Za-z_]\w*)"                                            # 7 declared name
    r"(\s*\[[^;\]]*\])?"                                         # 8 array suffix
    r"\s*(\=\s*[^;]*)?;\s*$")                                    # 9 optional initialiser
_POST_INC = re.compile(r"^(\s*)([A-Za-z_]\w*)\s*(\+\+|--)\s*;\s*$")
_PRE_INC = re.compile(r"^(\s*)(\+\+|--)\s*([A-Za-z_]\w*)\s*;\s*$")
_TOP_ASSIGN = re.compile(r"(?<![<>=!+\-*/%&|^])(?:<<|>>|[+\-*/%&|^])?=(?!=)")
_CALLEE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_NOT_CALLS = frozenset({"if", "while", "for", "switch", "sizeof", "return"})


def _mask_line(line: str, in_block: bool) -> tuple[str, bool, bool]:
    """Blank comments/strings (length preserved) so code columns stay aligned; track block comments."""
    out = list(line)
    i, n = 0, len(line)
    while i < n:
        if in_block:
            j = line.find("*/", i)
            if j < 0:
                out[i:] = [" "] * (n - i)
                return "".join(out), True, False
            out[i:j + 2] = [" "] * (j + 2 - i)
            in_block, i = False, j + 2
            continue
        c = line[i]
        if c in " \t\r":
            i += 1
            continue
        if line.startswith("//", i):
            out[i:] = [" "] * (n - i)
            break
        if line.startswith("/*", i):
            j = line.find("*/", i + 2)
            if j < 0:
                out[i:] = [" "] * (n - i)
                return "".join(out), True, False
            out[i:j + 2] = [" "] * (j + 2 - i)
            i = j + 2
            continue
        if c in "\"'":
            j = i + 1
            while j < n:
                if line[j] == "\\":
                    j += 2
                    continue
                if line[j] == c:
                    break
                j += 1
            if j >= n:
                out[i:] = [" "] * (n - i)
                return "".join(out), in_block, True  # unclosed literal: reported, never rewritten
            out[i:j + 1] = [" "] * (j + 1 - i)
            i = j + 1
            continue
        i += 1
    return "".join(out), in_block, False


def _top_level_matches(code: str, rx: re.Pattern) -> list[re.Match]:
    """Matches of `rx` whose start sits at parenthesis/bracket depth 0 (e.g. top-level `;`/assignments)."""
    depths, d = [], 0
    for ch in code:
        depths.append(d)
        if ch in "([":
            d += 1
        elif ch in ")]":
            d -= 1
    return [m for m in rx.finditer(code) if depths[m.start()] == 0]


def _match_paren(code: str, i: int) -> int:
    d = 0
    for j in range(i, len(code)):
        if code[j] == "(":
            d += 1
        elif code[j] == ")":
            d -= 1
            if d == 0:
                return j
    return -1


def _split_top(code: str, a: int, b: int, sep: str) -> list[tuple[int, int]]:
    segs, start, d = [], a, 0
    for j in range(a, b):
        ch = code[j]
        if ch in "([":
            d += 1
        elif ch in ")]":
            d -= 1
        elif ch == sep and d == 0:
            segs.append((start, j))
            start = j + 1
    segs.append((start, b))
    return segs


def _cond_spans(st: Statement) -> list[tuple[int, int]]:
    """Condition regions of same-line `if`/`while`/`for` constructs (for the middle clause of `for`)."""
    code = st.text
    spans: list[tuple[int, int]] = []
    for m in re.finditer(r"\b(if|while|for)\s*\(", code):
        close = _match_paren(code, m.end() - 1)
        if close < 0:
            continue
        if m.group(1) == "for":
            segs = _split_top(code, m.end(), close, ";")
            if len(segs) >= 3:
                spans.append(segs[1])
        else:
            spans.append((m.end() - 1, close + 1))  # include the parentheses for `(x == 0)` forms
    return spans


def _callees(code: str) -> frozenset[str]:
    return {m.group(1) for m in _CALLEE.finditer(code) if m.group(1) not in _NOT_CALLS}


def _ident_reads(code: str) -> frozenset[str]:
    """Identifiers read by an expression region: type keywords, callees and member names are excluded."""
    out = set()
    for m in re.finditer(r"\b([A-Za-z_]\w*)\b", code):
        name, i, j = m.group(1), m.start(), m.end()
        if name in _TYPE_KEYWORDS:
            continue
        if code[max(0, i - 2):i] == "->" or (i and code[i - 1] == "."):
            continue
        out.add(name)
    return frozenset(out - _callees(code))


def _has_memory(code: str) -> bool:
    """Possible memory access: member/subscript access, or a `*` used as dereference (not multiply)."""
    if re.search(r"->|\.|\[", code):
        return True
    prev = " "
    for ch in code:
        if ch == "*" and (prev == " " or prev in "=,([{;:?&|^~!<>=*/%+-"):
            return True
        if not ch.isspace():
            prev = ch
    return False


def _looks_like_body_open(masked: list[str], i: int, pos: int) -> bool:
    """A top-level `{` opens the function body if it follows a signature (`)`), not an initialiser (`=`)."""
    pre = masked[i][:pos].rstrip()
    if not pre:
        j = i - 1
        while j >= 0 and not masked[j].strip():
            j -= 1
        pre = masked[j].rstrip() if j >= 0 else ""
    if "=" in pre:
        return False
    return pre.endswith(")")


def _find_body(masked: list[str], unsupported: list[Unsupported]) -> tuple[int, int] | None:
    open_line = close_line = None
    depth, skipping = 0, 0
    for i, code in enumerate(masked):
        for pos, ch in enumerate(code):
            if ch == "{":
                if open_line is None and depth == 0:
                    if _looks_like_body_open(masked, i, pos):
                        open_line, depth = i, 1
                    else:
                        skipping, depth = 1, 1  # an initialiser/struct group: skipped, not the body
                else:
                    depth += 1
            elif ch == "}":
                depth -= 1
                if open_line is not None and depth == 0:
                    close_line = i
                    break
                if open_line is None and skipping and depth == 0:
                    skipping = 0
        if close_line is not None:
            break
    if open_line is None:
        return None
    if close_line is None:
        unsupported.append(Unsupported(Span.whole(masked, open_line, len(masked) - 1),
                                       "unbalanced braces: no closing brace for the function body"))
        return None
    return open_line, close_line


def _parse_params(code: str) -> dict[str, tuple[str, bool, bool]]:
    """Parameter types from a simple ANSI-style signature line (best effort; unnamed params are ignored)."""
    brace = code.find("{")
    pre = code[:brace] if brace >= 0 else code
    last = None
    for m in re.finditer(r"\(([^()]*)\)", pre):
        last = m
    if not last:
        return {}
    out: dict[str, tuple[str, bool, bool]] = {}
    for part in last.group(1).split(","):
        part = part.strip()
        if not part or part == "void":
            continue
        pm = re.fullmatch(r"((?:(?:unsigned|signed|const)\s+)*[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*)"
                          r"\s+(\**)\s*([A-Za-z_]\w*)(\s*\[[^\]]*\])?", part)
        if pm:
            out[pm.group(3)] = (pm.group(1).strip(), bool(pm.group(2)), bool(pm.group(4)))
    return out


def _classify(code: str, i: int, depth: int, scope: int, lines: list[str],
              volatile: set[str], types: dict[str, Statement],
              unsupported: list[Unsupported]) -> Statement:
    st = Statement(line=i, kind="blank", indent=len(code) - len(code.lstrip(" \t")),
                   depth=depth, scope=scope, text=code)
    if lines[i].lstrip().startswith("#"):
        unsupported.append(Unsupported(Span.whole(lines, i, i),
                                       "preprocessor directive inside the function body; never rewritten"))
        st.kind = "unsupported"
        return st
    stripped = code.strip()
    if not stripped or stripped == ";":
        return st
    ext = _EXTENSIONS.search(stripped)
    if ext:
        unsupported.append(Unsupported(Span.single(i, ext.start(), ext.end()),
                                       f"{ext.group(1)} (compiler extension / inline assembly) is not "
                                       "analysed; the line is never rewritten"))
        st.kind = "unsupported"
        return st
    kw = _CONTROL_KW.match(stripped)
    if kw:
        st.kind, st.kw = "control", kw.group(1)
        return st
    if stripped.startswith(("{", "}")):
        st.kind = "block"
        return st
    if _LABEL.match(code):
        st.kind = "label"
        return st
    if len(_top_level_matches(code, re.compile(";"))) > 1:
        unsupported.append(Unsupported(Span.whole(lines, i, i),
                                       "multiple statements on one line; the line is never rewritten"))
        st.kind = "unsupported"
        return st
    m = _DECL_STMT.match(code)
    if m:
        st.kind = "decl"
        st.name = m.group(7)
        st.type_text = " ".join([w for w in (m.group(3), m.group(4)) if w] + m.group(5).split())
        st.type_span = (m.start(3) if m.group(3) else m.start(4), m.end(5))
        st.qual_span = (m.start(2), m.end(2))
        st.pointer, st.array, st.has_init = bool(m.group(6)), bool(m.group(8)), bool(m.group(9))
        if "register" in m.group(2):
            st.kw = "register"
        if m.group(8):
            inner = m.group(8).strip("[] \t")
            if inner and not re.fullmatch(r"(?:0[xX][0-9A-Fa-f]+|\d+)[uUlL]*", inner):
                st.vla = True
                unsupported.append(Unsupported(Span.single(i, m.start(8), m.end(8)),
                                               f"variable-length array declarator on {st.name!r}; "
                                               "the declaration is never reordered"))
        if "volatile" in m.group(2):
            volatile.add(st.name)
        st.reorderable = not st.has_init and not st.vla
        types[st.name] = st
        return st
    mi = _POST_INC.match(code)
    if mi:
        st.kind = "incdec"
        st.writes = st.reads = frozenset({mi.group(2)})
        st.volatile = mi.group(2) in volatile
        return st
    mi = _PRE_INC.match(code)
    if mi:
        st.kind = "incdec"
        st.writes = st.reads = frozenset({mi.group(3)})
        st.volatile = mi.group(3) in volatile
        return st
    assign = _top_level_matches(code, _TOP_ASSIGN)
    if assign:
        m = assign[0]
        lhs, rhs = code[:m.start()], code[m.end():]
        lhs_m = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*", lhs)
        if lhs_m:
            st.kind = "compound" if m.group(0) != "=" else "assign"
            st.writes = frozenset({lhs_m.group(1)})
            st.reads = _ident_reads(rhs) | (st.writes if st.kind == "compound" else frozenset())
            st.memory = _has_memory(rhs)
        else:
            st.kind = "assign"
            st.memory = True
            st.reads = _ident_reads(rhs)
        st.calls = bool(_callees(code))
        st.volatile = "volatile" in code or bool((st.writes | st.reads) & volatile)
        return st
    st.kind = "expr"
    st.reads = _ident_reads(code)
    st.calls = bool(_callees(code))
    st.memory = _has_memory(code)
    st.volatile = "volatile" in code or bool(st.reads & volatile)
    return st


def analyze(source: str) -> Analysis:
    """Token/line-level analysis of an admitted old-C function body (plan P5.2, no parser dependency)."""
    lines = source.split("\n")
    masked, unsupported = [], []
    in_block = False
    for i, line in enumerate(lines):
        m, in_block, unclosed = _mask_line(line, in_block)
        masked.append(m)
        if unclosed:
            unsupported.append(Unsupported(Span.whole(lines, i, i),
                                           "unclosed string or char literal; the line is never rewritten"))
    if in_block:
        unsupported.append(Unsupported(Span.whole(lines, len(lines) - 1, len(lines) - 1),
                                       "unclosed block comment; remaining lines are never rewritten"))
    for i, line in enumerate(lines):
        if masked[i].lstrip().startswith("#"):
            masked[i] = " " * len(lines[i])  # preprocessor lines never contribute braces or statements
    found = _find_body(masked, unsupported)
    if found is None:
        return Analysis(source, lines, None, {}, {}, {}, frozenset(), frozenset(), unsupported)
    open_line, close_line = found
    lo, hi = open_line + 1, close_line
    address_taken: set[str] = set()
    for i in range(lo, hi):
        for m in re.finditer(r"&\s*([A-Za-z_]\w*)", masked[i]):
            address_taken.add(m.group(1))
    stmts: dict[int, Statement] = {}
    types: dict[str, Statement] = {}
    volatile: set[str] = set()
    stack = [0]
    next_scope = 1
    for i in range(lo, hi):
        code = masked[i]
        st = _classify(code, i, len(stack), stack[-1], lines, volatile, types, unsupported)
        stmts[i] = st
        for ch in code:
            if ch == "{":
                stack.append(next_scope)
                next_scope += 1
            elif ch == "}" and len(stack) > 1:
                stack.pop()
    return Analysis(source, lines, (lo, hi), stmts, types, _parse_params(masked[open_line]),
                    frozenset(volatile), frozenset(address_taken), unsupported)


# ------------------------------------------------- dependency checks (P5.2/P5.3)

_SWAPPABLE_KINDS = frozenset({"assign", "compound", "incdec", "expr"})


def _is_int_scalar(analysis: Analysis, name: str) -> bool:
    """`name` is a locally declared (or parameter) integer scalar: the precondition for type-sensitive ops."""
    if name in analysis.decls:
        st = analysis.decls[name]
        if st.pointer or st.array or not st.type_text:
            return False
        words = st.type_text.lower().split()
    elif name in analysis.params:
        t, ptr, arr = analysis.params[name]
        if ptr or arr or not t:
            return False
        words = t.lower().split()
    else:
        return False
    return not any(w in _FLOAT_WORDS for w in words)


def swap_allowed(analysis: Analysis, a: Statement, b: Statement) -> bool:
    """Two adjacent statements may be reordered as a restricted-equivalence rewrite.

    Requires disjoint local reads/writes, no volatile accesses, unknown calls, or pointer/array/member
    accesses, the same scope, and at least one write. Aliasing through globals or taken addresses is
    assumed absent (an explicit, recorded assumption - not a promise of full C equivalence)."""
    if a.kind not in _SWAPPABLE_KINDS or b.kind not in _SWAPPABLE_KINDS:
        return False
    if a.indent != b.indent or a.scope != b.scope:
        return False
    if a.calls or a.memory or a.volatile or b.calls or b.memory or b.volatile:
        return False
    if not (a.writes or b.writes):
        return False
    return not ((a.writes & b.reads) or (b.writes & a.reads) or (a.writes & b.writes))


def _span_lines(lines: list[str], first: int, last: int) -> Span:
    return Span.whole(lines, first, last)


# ------------------------------------------------- operators (P5.3, restricted)


@_op("swap_statements", RESTRICTED,
     "adjacent statements with disjoint local reads/writes; no volatile accesses, unknown calls, or "
     "pointer/array/member accesses; assumes no aliasing of globals")
def _op_swap_statements(analysis: Analysis, rng: random.Random):
    lo, hi = analysis.body
    pairs = [(i, i + 1) for i in range(lo, hi - 1)
             if swap_allowed(analysis, analysis.stmts[i], analysis.stmts[i + 1])]
    if not pairs:
        return None
    i, j = rng.choice(pairs)
    new = list(analysis.lines)
    new[i], new[j] = new[j], new[i]
    return new, (_span_lines(analysis.lines, i, j),)


@_op("reorder_decls", RESTRICTED,
     "uninitialised declarations without variable-length array declarators, reordered inside one scope")
def _op_reorder_decls(analysis: Analysis, rng: random.Random):
    groups: dict[tuple[int, int], list[Statement]] = {}
    for st in analysis.stmts.values():
        if st.kind == "decl" and st.reorderable and not st.vla:
            groups.setdefault((st.scope, st.indent), []).append(st)
    groups = [g for g in groups.values() if len(g) >= 2]
    if not groups:
        return None
    a, b = rng.sample(rng.choice(groups), 2)
    new = list(analysis.lines)
    new[a.line], new[b.line] = new[b.line], new[a.line]
    return new, (_span_lines(analysis.lines, a.line, a.line), _span_lines(analysis.lines, b.line, b.line))


_BINOP = re.compile(r"^(\s*)([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*|\d+)\s*"
                    r"(\+|-|\*|/|%|<<|>>|&|\||\^)\s*([A-Za-z_]\w*|\d+)\s*;\s*$")
_SPLIT_A = re.compile(r"^(\s*)([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*|\d+)\s*;\s*$")
_SPLIT_B = re.compile(r"^(\s*)([A-Za-z_]\w*)\s*(\+=|-=|\*=|/=|%=|<<=|>>=|&=|\|=|\^=)\s*"
                      r"([A-Za-z_]\w*|\d+)\s*;\s*$")
_COMPOUND_BASE = {"+=": "+", "-=": "-", "*=": "*", "/=": "/", "%=": "%", "<<=": "<<",
                  ">>=": ">>", "&=": "&", "|=": "|", "^=": "^"}


def _binop_ok(analysis: Analysis, x: str, a: str, b: str) -> bool:
    """Type handling for temporary split/combine: integer scalars only, right operand independent of x."""
    if b == x:
        return False  # b is evaluated after x changes in the split form
    if x in analysis.volatile_names or a in analysis.volatile_names or b in analysis.volatile_names:
        return False
    if not _is_int_scalar(analysis, x):
        return False
    for operand in (a, b):
        if operand.isdigit():
            continue
        if not _is_int_scalar(analysis, operand):
            return False
    return True


@_op("temp_introduce", RESTRICTED,
     "introduce a temporary: integer-typed locals only; the right operand never reads the target, so "
     "evaluation stays controlled")
def _op_temp_introduce(analysis: Analysis, rng: random.Random):
    options = []
    for st in analysis.stmts.values():
        if st.kind != "assign":
            continue
        m = _BINOP.match(st.text)
        if not m or not _binop_ok(analysis, m.group(2), m.group(3), m.group(5)):
            continue
        if m.group(3) == m.group(2):
            continue  # `x = x;` produces nothing useful
        options.append((st, m))
    if not options:
        return None
    st, m = rng.choice(options)
    x, a, op, b = m.group(2), m.group(3), m.group(4), m.group(5)
    new = list(analysis.lines)
    new[st.line] = f"{m.group(1)}{x} = {a};"
    new.insert(st.line + 1, f"{m.group(1)}{x} {op}= {b};")
    return new, (_span_lines(analysis.lines, st.line, st.line),)


@_op("temp_eliminate", RESTRICTED,
     "eliminate a temporary: merges `x = a; x op= b;` back into one expression when b never reads x")
def _op_temp_eliminate(analysis: Analysis, rng: random.Random):
    lo, hi = analysis.body
    options = []
    for i in range(lo, hi - 1):
        a, b = analysis.stmts[i], analysis.stmts[i + 1]
        if a.kind != "assign" or b.kind != "compound":
            continue
        ma, mb = _SPLIT_A.match(a.text), _SPLIT_B.match(b.text)
        if not ma or not mb or ma.group(2) != mb.group(2) or ma.group(1) != mb.group(1):
            continue
        if not _binop_ok(analysis, ma.group(2), ma.group(3), mb.group(4)):
            continue
        options.append((i, ma, mb))
    if not options:
        return None
    i, ma, mb = rng.choice(options)
    new = list(analysis.lines)
    new[i] = f"{ma.group(1)}{ma.group(2)} = {ma.group(3)} {_COMPOUND_BASE[mb.group(3)]} {mb.group(4)};"
    del new[i + 1]
    return new, (_span_lines(analysis.lines, i, i + 1),)


@_op("inc_style", RESTRICTED,
     "standalone `x++` / `++x` / `x += 1` forms of one statement; target is a plain non-volatile identifier")
def _op_inc_style(analysis: Analysis, rng: random.Random):
    options = []
    for st in analysis.stmts.values():
        if st.kind not in ("incdec", "compound") or st.volatile:
            continue
        m = _POST_INC.match(st.text)
        if m:
            sign = "+" if m.group(3) == "++" else "-"
            options.append((st, m, [f"{m.group(1)}{m.group(2)} {sign}= 1;",
                                    f"{m.group(1)}{m.group(3)}{m.group(2)};"]))
            continue
        m = _PRE_INC.match(st.text)
        if m:
            sign = "+" if m.group(2) == "++" else "-"
            options.append((st, m, [f"{m.group(1)}{m.group(3)} {sign}= 1;",
                                    f"{m.group(1)}{m.group(3)}{m.group(2)};"]))
            continue
        m = re.fullmatch(r"(\s*)([A-Za-z_]\w*)\s*(\+=|-=)\s*1\s*;\s*", st.text)
        if m:
            op = "++" if m.group(3) == "+=" else "--"
            options.append((st, m, [f"{m.group(1)}{op}{m.group(2)};", f"{m.group(1)}{m.group(2)}{op};"]))
    if not options:
        return None
    st, m, forms = rng.choice(options)
    new = list(analysis.lines)
    new[st.line] = new[st.line][:m.start()] + rng.choice(forms) + new[st.line][m.end():]
    return new, (Span.single(st.line, m.start(), m.end()),)


_FLIP = re.compile(r"([A-Za-z_]\w*|\d+)\s*(<=|>=|<|>)(?![<>=])\s*([A-Za-z_]\w*|\d+)")
_FLIP_MAP = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}


@_op("comparison_flip", RESTRICTED,
     "direction-reversed comparison in a control condition; pure operand reads only, and unordered (NaN) "
     "comparisons keep the same truth value")
def _op_comparison_flip(analysis: Analysis, rng: random.Random):
    options = []
    for st in analysis.stmts.values():
        if st.kind != "control" or st.kw not in ("if", "while", "for"):
            continue
        for a, b in _cond_spans(st):
            for m in _FLIP.finditer(st.text, a, b):
                if m.group(1) in analysis.volatile_names or m.group(3) in analysis.volatile_names:
                    continue
                options.append((st, m))
    if not options:
        return None
    st, m = rng.choice(options)
    new = list(analysis.lines)
    repl = f"{m.group(3)} {_FLIP_MAP[m.group(2)]} {m.group(1)}"
    new[st.line] = new[st.line][:m.start()] + repl + new[st.line][m.end():]
    return new, (Span.single(st.line, m.start(), m.end()),)


_BOOL_FORMS = [
    (re.compile(r"\(!\s*(\w+)\s*\)"), lambda m: f"({m.group(1)} == 0)"),
    (re.compile(r"\((\w+)\s*==\s*0\s*\)"), lambda m: f"(!{m.group(1)})"),
    (re.compile(r"\((\w+)\s*!=\s*0\s*\)"), lambda m: f"({m.group(1)})"),
]


@_op("cond_bool_form", RESTRICTED,
     "equivalent condition form (`!x` vs `x == 0`, `x != 0` vs `x`) where the operand is an integer "
     "scalar, so floating-point NaN semantics cannot change the condition")
def _op_cond_bool_form(analysis: Analysis, rng: random.Random):
    options = []
    for st in analysis.stmts.values():
        if st.kind != "control" or st.kw not in ("if", "while", "for"):
            continue
        for a, b in _cond_spans(st):
            for rx, fn in _BOOL_FORMS:
                for m in rx.finditer(st.text, a, b):
                    if m.group(1) in analysis.volatile_names or not _is_int_scalar(analysis, m.group(1)):
                        continue
                    options.append((st, m, fn))
    if not options:
        return None
    st, m, fn = rng.choice(options)
    new = list(analysis.lines)
    new[st.line] = new[st.line][:m.start()] + fn(m) + new[st.line][m.end():]
    return new, (Span.single(st.line, m.start(), m.end()),)


@_op("while_for", RESTRICTED,
     "`while (c)` and `for (; c;)` are the same loop in C89/C99; conditions with calls or `;` are refused")
def _op_while_for(analysis: Analysis, rng: random.Random):
    options = []
    for st in analysis.stmts.values():
        if st.kind != "control":
            continue
        m = re.fullmatch(r"(\s*)while\s*\(([^;()]*)\)(\s*)(\{?)\s*", st.text)
        if m and st.kw == "while" and m.group(2).strip():
            options.append((st, f"{m.group(1)}for (; {m.group(2).strip()};){m.group(3)}{m.group(4)}",
                            m.span()))
            continue
        m = re.fullmatch(r"(\s*)for\s*\(\s*;\s*([^;()]*)\s*;\s*\)(\s*)(\{?)\s*", st.text)
        if m and st.kw == "for" and m.group(2).strip():
            options.append((st, f"{m.group(1)}while ({m.group(2).strip()}){m.group(3)}{m.group(4)}",
                            m.span()))
    if not options:
        return None
    st, repl, span = rng.choice(options)
    new = list(analysis.lines)
    new[st.line] = new[st.line][:span[0]] + repl + new[st.line][span[1]:]
    return new, (Span.single(st.line, span[0], span[1]),)


@_op("register_toggle", RESTRICTED,
     "toggle the `register` storage hint; a pure code-generation hint in C, added only when the address "
     "is never taken")
def _op_register_toggle(analysis: Analysis, rng: random.Random):
    options = []
    for st in analysis.stmts.values():
        if st.kind != "decl" or st.array:
            continue
        m = re.search(r"register\s+", st.text[st.qual_span[0]:st.qual_span[1]]) if st.qual_span else None
        if m:
            pos = st.qual_span[0] + m.start()
            options.append((st, pos, pos + m.end() - m.start(), ""))
        elif st.name not in analysis.address_taken:
            tstart = st.type_span[0] if st.type_span else 0
            options.append((st, tstart, tstart, "register "))
    if not options:
        return None
    st, start, end, repl = rng.choice(options)
    new = list(analysis.lines)
    new[st.line] = new[st.line][:start] + repl + new[st.line][end:]
    return new, (Span.single(st.line, start, start + len(repl) if repl else end),)


# ------------------------------------------------- operators (P5.3, exploratory)

@_op("retype_local", EXPLORATORY,
     "exploratory: test a different signedness/type interpretation for a local; the judge decides")
def _op_retype_local(analysis: Analysis, rng: random.Random):
    cands = [st for st in analysis.stmts.values()
             if st.kind == "decl" and st.type_text in TYPES_32 and not st.pointer and not st.array]
    if not cands:
        return None
    st = rng.choice(cands)
    new_type = rng.choice([t for t in TYPES_32 if t != st.type_text])
    new = list(analysis.lines)
    new[st.line] = (new[st.line][:st.type_span[0]] + new_type + new[st.line][st.type_span[1]:])
    return new, (Span.single(st.line, st.type_span[0], st.type_span[1]),)


@_op("cast_add", EXPLORATORY,
     "exploratory: insert a cast at an rvalue position; the judge decides")
def _op_cast_add(analysis: Analysis, rng: random.Random):
    options = []
    for st in analysis.stmts.values():
        if st.kind in ("assign", "compound"):
            assign = _top_level_matches(st.text, _TOP_ASSIGN)
            region, offset = st.text[assign[0].end():], assign[0].end()
        elif st.kind == "control" and st.kw == "return":
            at = st.text.find("return") + len("return")
            region, offset = st.text[at:], at
        else:
            continue
        for m in re.finditer(r"\b([A-Za-z_]\w*)\b", region):
            name, pos = m.group(1), offset + m.start()
            if name in _TYPE_KEYWORDS:
                continue
            code, j = st.text, pos + len(name)
            if pos >= 2 and code[pos - 2:pos] == "->":
                continue
            if pos >= 1 and code[pos - 1] == ".":
                continue
            nxt = code[j:j + 2]
            if j < len(code) and code[j] in "(.[":
                continue
            if nxt in ("++", "--"):
                continue
            if j < len(code) and code[j] == "=":
                continue
            if name in _callees(st.text):
                continue
            options.append((st, pos))
    if not options:
        return None
    st, pos = rng.choice(options)
    cast = rng.choice(CASTS)
    new = list(analysis.lines)
    new[st.line] = new[st.line][:pos] + cast + new[st.line][pos:]
    return new, (Span.single(st.line, pos, pos + len(cast)),)


_CAST = re.compile(r"\(\s*(?:unsigned\s+short\s+int|unsigned\s+long\s+int|unsigned\s+short|"
                   r"unsigned\s+long|unsigned\s+int|unsigned|short\s+int|short|long\s+int|long|"
                   r"int|char|s32|u32|s16|u16|s8|u8|uInt|uLong)\s*\)\s*(?=[A-Za-z_]\w*)")


@_op("cast_remove", EXPLORATORY,
     "exploratory: remove a cast before an identifier; the judge decides")
def _op_cast_remove(analysis: Analysis, rng: random.Random):
    options = []
    for st in analysis.stmts.values():
        if st.kind in ("blank", "unsupported", "block", "label"):
            continue
        for m in _CAST.finditer(st.text):
            options.append((st, m.span()))
    if not options:
        return None
    st, (start, end) = rng.choice(options)
    new = list(analysis.lines)
    new[st.line] = new[st.line][:start] + new[st.line][end:]
    return new, (Span.single(st.line, start, end),)


_IF_RET = re.compile(r"^(\s*)if\s*\((!?\s*\w+)\)\s+(?:return\s+(\w+|\d+)\s*;"
                     r"|\{\s*return\s+(\w+|\d+)\s*;\s*\})\s*$")
_RET = re.compile(r"^(\s*)return\s+(\w+|\d+)\s*;\s*$")
_TERN = re.compile(r"^(\s*)return\s+(!?\s*\w+)\s*\?\s*(\w+|\d+)\s*:\s*(\w+|\d+)\s*;\s*$")


@_op("return_ternary", EXPLORATORY,
     "exploratory: reshape `if (c) return a; return b;` to/from a ternary return; a different "
     "control-flow interpretation, the judge decides")
def _op_return_ternary(analysis: Analysis, rng: random.Random):
    lo, hi = analysis.body
    options = []
    for i in range(lo, hi):
        st = analysis.stmts[i]
        if st.kind != "control":
            continue
        m = _IF_RET.match(st.text)
        if m and st.kw == "if" and i + 1 < hi:
            nxt = analysis.stmts[i + 1]
            mr = _RET.match(nxt.text)
            if nxt.kind == "control" and nxt.kw == "return" and mr and mr.group(1) == m.group(1):
                options.append(("merge", i, m, mr))
                continue
        m = _TERN.match(st.text)
        if m and st.kw == "return":
            options.append(("split", i, m, None))
    if not options:
        return None
    new = list(analysis.lines)
    choice = rng.choice(options)
    if choice[0] == "merge":
        _, i, m, mr = choice
        a = m.group(3) or m.group(4)
        new[i] = f"{m.group(1)}return {m.group(2).strip()} ? {a} : {mr.group(2)};"
        del new[i + 1]
        return new, (_span_lines(analysis.lines, i, i + 1),)
    _, i, m, _ = choice
    new[i] = f"{m.group(1)}if ({m.group(2).strip()}) return {m.group(3)};"
    new.insert(i + 1, f"{m.group(1)}return {m.group(4)};")
    return new, (_span_lines(analysis.lines, i, i),)


@_op("branch_swap", EXPLORATORY,
     "exploratory: swap `if (c) { A } else { B }` branches and negate the condition; a different "
     "control-flow interpretation, the judge decides")
def _op_branch_swap(analysis: Analysis, rng: random.Random):
    options = []
    for st in analysis.stmts.values():
        if st.kind != "control" or st.kw != "if":
            continue
        m = re.fullmatch(r"(\s*)if\s*\((!?\s*\w+)\)\s*\{([^{}]*)\}\s*else\s*\{([^{}]*)\}\s*", st.text)
        if not m:
            m = re.fullmatch(r"(\s*)if\s*\((!?\s*\w+)\)\s*([^{};]+);\s*else\s*([^{};]+);\s*", st.text)
            if not m:
                continue
            orig = analysis.lines[st.line]
            a, b = orig[m.start(3):m.end(3)], orig[m.start(4):m.end(4)]
            if a.strip() == b.strip():
                continue
            options.append((st, m, f"if ({_negate(m.group(2))}) {b}; else {a};", m.span()))
        else:
            orig = analysis.lines[st.line]
            a, b = orig[m.start(3):m.end(3)], orig[m.start(4):m.end(4)]
            if a.strip() == b.strip():
                continue
            options.append((st, m, f"if ({_negate(m.group(2))}) {{{b}}} else {{{a}}}", m.span()))
    if not options:
        return None
    st, m, repl, span = rng.choice(options)
    new = list(analysis.lines)
    new[st.line] = new[st.line][:span[0]] + m.group(1) + repl + new[st.line][span[1]:]
    return new, (Span.single(st.line, span[0], span[1]),)


def _negate(cond: str) -> str:
    cond = cond.strip()
    return cond[1:].strip() if cond.startswith("!") else f"!{cond}"


_BOUND = re.compile(r"([A-Za-z_]\w*|\d+)\s*(<=|>=|<|>)(?![<>=])\s*([A-Za-z_]\w*|\d+)")
_BOUND_EXPLORE = {"<": ("!=", "<="), "<=": ("!=", "<"), ">": ("!=", ">="), ">=": ("!=", ">")}


@_op("loop_bound_explore", EXPLORATORY,
     "exploratory: probe a different loop-bound interpretation (`<` vs `!=` vs `<=`); the judge decides")
def _op_loop_bound_explore(analysis: Analysis, rng: random.Random):
    options = []
    for st in analysis.stmts.values():
        if st.kind != "control" or st.kw not in ("if", "while", "for"):
            continue
        for a, b in _cond_spans(st):
            for m in _BOUND.finditer(st.text, a, b):
                options.append((st, m))
    if not options:
        return None
    st, m = rng.choice(options)
    new_op = rng.choice(_BOUND_EXPLORE[m.group(2)])
    new = list(analysis.lines)
    repl = f"{m.group(1)} {new_op} {m.group(3)}"
    new[st.line] = new[st.line][:m.start()] + repl + new[st.line][m.end():]
    return new, (Span.single(st.line, m.start(), m.end()),)


# ------------------------------------------------------- policy and entry points

DEFAULT_POLICY: dict[str, int] = {
    "reorder_decls": 1,
    "swap_statements": 2,
    "temp_introduce": 1,
    "temp_eliminate": 1,
    "inc_style": 1,
    "comparison_flip": 1,
    "cond_bool_form": 1,
    "while_for": 1,
    "register_toggle": 1,
    "retype_local": 1,
    "cast_add": 2,
    "cast_remove": 2,
    "return_ternary": 1,
    "branch_swap": 1,
    "loop_bound_explore": 1,
}


def propose(source: str, rng: random.Random, policy: dict[str, int] | None = None) -> Mutation | None:
    """One labelled mutation with its provenance, or None when no operator applies."""
    if policy is None:
        policy = DEFAULT_POLICY
    analysis = analyze(source)
    if analysis.body is None:
        return _fallback_propose(source, rng)
    ops = [OPERATORS[op_id] for op_id, weight in policy.items() if weight > 0 and op_id in OPERATORS]
    if not ops:
        return None
    weights = [policy[op.id] for op in ops]
    for _ in range(6):
        op = rng.choices(ops, weights=weights, k=1)[0]
        res = op.apply(analysis, rng)
        if res is None:
            continue
        new_lines, spans = res
        text = "\n".join(new_lines)
        if text == source:
            continue
        return Mutation(op.id, op.category, digest(source), digest(text), tuple(spans), text, op.note)
    return None


def mutate(source: str, rng: random.Random, policy: dict[str, int] | None = None,
           record: list[Mutation] | None = None) -> str | None:
    """Apply one mutation and return the new source (the caller's contract), or None when nothing applies.

    `record`, when given, collects the Mutation provenance (parent digest, operator, category, spans)
    for reproducible search runs (P5.5/P5.6 groundwork)."""
    m = propose(source, rng, policy)
    if m is None:
        return None
    if record is not None:
        record.append(m)
    return m.text


# ----------------------------------------------- legacy fallback (controlled, labelled)

# the pre-P5 text engine, kept only for sources the analyser cannot structure (no recognisable body).
# Every mutation it produces is labelled FALLBACK and never claims equivalence (plan P5.2 requirement 4).
_LEGACY_DECL = re.compile(r"^(\s+)(register\s+)?((?:unsigned\s+|signed\s+)?\w+)(\s*\**\s*)(\w+)(\s*(?:=[^;]*)?;)\s*$")
_LEGACY_STMT = re.compile(r"^\s+[^\s{}#][^{}]*;\s*$")
_LEGACY_CONTROL = re.compile(r"\b(return|break|continue|goto|case|default)\b|^\s*(else|for|while|do|switch)\b")
_LEGACY_REWRITES = [
    (re.compile(r"\b(\w+)\+\+;"), lambda m: f"{m.group(1)} += 1;"),
    (re.compile(r"\b(\w+) \+= 1;"), lambda m: f"++{m.group(1)};"),
    (re.compile(r"\+\+(\w+);"), lambda m: f"{m.group(1)}++;"),
    (re.compile(r"\b(\w+)--;"), lambda m: f"{m.group(1)} -= 1;"),
    (re.compile(r"\b(\w+) -= 1;"), lambda m: f"{m.group(1)}--;"),
    (re.compile(r"\((\w+) < (\w+)\)"), lambda m: f"({m.group(2)} > {m.group(1)})"),
    (re.compile(r"\((\w+) > (\w+)\)"), lambda m: f"({m.group(2)} < {m.group(1)})"),
    (re.compile(r"\((\w+) <= (\w+)\)"), lambda m: f"({m.group(2)} >= {m.group(1)})"),
    (re.compile(r"\((\w+) >= (\w+)\)"), lambda m: f"({m.group(2)} <= {m.group(1)})"),
    (re.compile(r"\(!(\w+)\)"), lambda m: f"({m.group(1)} == 0)"),
    (re.compile(r"\((\w+) == 0\)"), lambda m: f"(!{m.group(1)})"),
    (re.compile(r"\((\w+) != 0\)"), lambda m: f"({m.group(1)})"),
    (re.compile(r"\(\s*(?:unsigned short|unsigned|short|int|s32|u32|uInt|uLong)\s*\)\s*(?=\w)"), lambda m: ""),
]


def _body_bounds(lines: list[str]) -> tuple[int, int]:
    start = next((i for i, l in enumerate(lines) if "{" in l), 0)
    end = max((i for i, l in enumerate(lines) if l.strip().startswith("}")), default=len(lines) - 1)
    return start + 1, end


def _legacy_swap(lines, rng):
    lo, hi = _body_bounds(lines)
    pairs = [i for i in range(lo, hi - 1) if _LEGACY_STMT.match(lines[i]) and _LEGACY_STMT.match(lines[i + 1])
             and not _LEGACY_CONTROL.search(lines[i] + lines[i + 1]) and not _LEGACY_DECL.match(lines[i])
             and lines[i][:len(lines[i]) - len(lines[i].lstrip())] == lines[i + 1][:len(lines[i + 1]) - len(lines[i + 1].lstrip())]]
    if not pairs:
        return None
    i = rng.choice(pairs)
    lines[i], lines[i + 1] = lines[i + 1], lines[i]
    return lines, _span_lines(lines, i, i + 1)


def _legacy_shuffle(lines, rng):
    lo, hi = _body_bounds(lines)
    run = []
    for i in range(lo, hi):
        if _LEGACY_DECL.match(lines[i]) and "=" not in lines[i]:
            run.append(i)
        elif run:
            break
    if len(run) < 2:
        return None
    a, b = rng.sample(run, 2)
    lines[a], lines[b] = lines[b], lines[a]
    return lines, (_span_lines(lines, a, a), _span_lines(lines, b, b))


def _legacy_retype(lines, rng):
    lo, hi = _body_bounds(lines)
    cands = [i for i in range(lo, hi) if (m := _LEGACY_DECL.match(lines[i])) and m.group(3) in TYPES_32
             and not m.group(4).strip()]
    if not cands:
        return None
    i = rng.choice(cands)
    m = _LEGACY_DECL.match(lines[i])
    lines[i] = (f"{m.group(1)}{m.group(2) or ''}{rng.choice([t for t in TYPES_32 if t != m.group(3)])}"
                f"{m.group(4)}{m.group(5)}{m.group(6)}")
    return lines, _span_lines(lines, i, i)


def _legacy_register(lines, rng):
    lo, hi = _body_bounds(lines)
    cands = [i for i in range(lo, hi) if _LEGACY_DECL.match(lines[i])]
    if not cands:
        return None
    i = rng.choice(cands)
    m = _LEGACY_DECL.match(lines[i])
    reg = "" if m.group(2) else "register "
    lines[i] = f"{m.group(1)}{reg}{m.group(3)}{m.group(4)}{m.group(5)}{m.group(6)}"
    return lines, _span_lines(lines, i, i)


def _legacy_rewrite(lines, rng):
    lo, hi = _body_bounds(lines)
    options = [(i, rx, fn) for i in range(lo, hi) for rx, fn in _LEGACY_REWRITES if rx.search(lines[i])]
    if not options:
        return None
    i, rx, fn = rng.choice(options)
    m = rng.choice(list(rx.finditer(lines[i])))
    span = Span.single(i, m.start(), m.end())
    lines[i] = lines[i][:m.start()] + fn(m) + lines[i][m.end():]
    return lines, span


def _legacy_add_cast(lines, rng):
    lo, hi = _body_bounds(lines)
    cands = [(i, m) for i in range(lo, hi) if not _LEGACY_DECL.match(lines[i]) and "=" in lines[i]
             for m in re.finditer(r"(?<=[=(,+\-*/&|^ ])(\w+)\b(?!\s*(?:\(|=(?!=)|\+\+|--|\[|\.|->|[-+*/&|^]=))",
                                  lines[i]) if not m.group(1).isdigit()]
    if not cands:
        return None
    i, m = rng.choice(cands)
    cast = rng.choice(CASTS)
    lines[i] = lines[i][:m.start()] + cast + lines[i][m.start():]
    return lines, Span.single(i, m.start(), m.start() + len(cast))


_LEGACY = [("swap_statements", _legacy_swap),
           ("shuffle_declarations", _legacy_shuffle),
           ("retype_local", _legacy_retype),
           ("toggle_register", _legacy_register),
           ("rewrite", _legacy_rewrite),
           ("rewrite", _legacy_rewrite),
           ("add_cast", _legacy_add_cast)]


def _fallback_propose(source: str, rng: random.Random) -> Mutation | None:
    lines = source.split("\n")
    for _ in range(6):
        op_id, fn = rng.choice(_LEGACY)
        res = fn(list(lines), rng)
        if res is None:
            continue
        new_lines, span = res
        if new_lines == lines:
            continue
        text = "\n".join(new_lines)
        return Mutation(f"legacy:{op_id}", FALLBACK, digest(source), digest(text), (span,), text,
                        "legacy text engine, used only because the analyser could not structure this "
                        "source; labelled fallback, no equivalence claim")
    return None


class LocalScorer:
    """Compile + score one job's sources on this machine, exactly like the coordinator does."""

    def __init__(self, job: dict, log=print):
        tc = toolchains.get(job["toolchain"])
        if toolchains.installed(tc.id) is None:
            if tc.proprietary:
                raise toolchains.ToolchainError(f"{tc.id} is proprietary; install it yourself if you accept its license")
            toolchains.install(tc.id, log=log)
        self._tmp = tempfile.TemporaryDirectory(prefix="decompnet-worker-")
        inc = Path(self._tmp.name)
        for rel, text in job.get("include_files", {}).items():
            dest = (inc / rel).resolve()
            if inc.resolve() not in dest.parents:
                raise ValueError(f"refusing include path {rel!r}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8", newline="\n")
        # the coordinator's own way to run the toolchain (on Windows, Linux-only ones go through WSL)
        compiler = {"toolchain": tc.id, "include_dirs": [str(inc)]}
        compiler.update({k: job[k] for k in ("cmd", "flags") if job.get(k)})
        self.cc = compiler_config(inc, {"compiler": compiler})
        t = job["target"]
        self.target = FunctionObj(job["function"], t["arch"], t["big_endian"], base64.b64decode(t["code"]),
                                  [Reloc.from_json(r) for r in t["relocs"]])
        self.context, self.name = job["context"], job["function"]

    def score(self, source: str) -> int | None:
        res = compile_source(self.cc, self.context, source)
        cand = extract_function(res.obj, self.name) if res.ok else None
        return scoring.diff(self.target, cand).score if cand else None


def search(job: dict, seconds: float, on_improvement, log=print, seed: int | None = None) -> tuple[int, str]:
    rng = random.Random(seed)
    scorer = LocalScorer(job, log)
    best_src = cur_src = job["source"]
    best = cur = scorer.score(best_src)
    if best is None:
        log("  the coordinator's best source doesn't compile here; skipping")
        return job["best_score"], best_src
    deadline, tries = time.monotonic() + seconds, 0
    while time.monotonic() < deadline and best > 0:
        cand = mutate(cur_src if rng.random() < 0.85 else best_src, rng)
        if cand is None:
            continue
        tries += 1
        s = scorer.score(cand)
        if s is None:
            continue
        if s < best:
            best, best_src = s, cand
            log(f"  try {tries}: score {s}")
            on_improvement(s, cand)
        if s <= cur or rng.random() < 0.02:  # mostly downhill, occasionally sideways to escape plateaus
            cur, cur_src = s, cand
    log(f"  {tries} variants tried, best {best}")
    return best, best_src


def _api(server: str, token: str, path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(server.rstrip("/") + path, data=json.dumps(payload).encode() if payload else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                           "User-Agent": "decompnet-worker"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)  # flush: the installer runs workers with output in a log file


def worker(server: str, token: str, minutes_per_job: float = 10, max_jobs: int = 0, log=_log) -> None:
    done = 0
    while not max_jobs or done < max_jobs:
        job = _api(server, token, "/api/jobs/next").get("job")
        if not job:
            log("no near-matches need work right now; checking again in 5 minutes")
            time.sleep(300)
            continue
        log(f"job: {job['project']}/{job['function']} (best score {job['best_score']}, {job['toolchain']})")
        submitted = {"best": job["best_score"]}

        def report(score: int, source: str) -> None:
            if score < submitted["best"]:
                verdict = _api(server, token, "/api/jobs/submit",
                               {"project": job["project"], "function": job["function"], "source": source})
                log(f"  submitted score {score}: coordinator says {verdict}")
                if verdict.get("accepted"):
                    submitted["best"] = verdict.get("score", score)

        try:
            search(job, minutes_per_job * 60, report, log)
        except toolchains.ToolchainError as e:
            log(f"  skipped: {e}")
        done += 1
