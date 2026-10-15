"""Disassembly, instruction diffing and decomp.me/asm-differ style scoring.

score == 0 means a byte-for-byte (relocation-normalized) match. Lower is better.

Phase 3 additions (plan P3.1-P3.4, tests S-01..S-07):

- `prepare()` builds a `PreparedTarget` once per target identity: decoded instructions
  (offsets, bytes and operand structure), a relocation lookup indexed by instruction
  interval (multiple relocations per instruction, variable-width instructions), the
  normalised fingerprint kept distinct from the exact-byte digest, the mnemonic sequence
  and the instruction count. The cache is keyed by target bytes, relocation metadata
  (which carries the symbol-resolution data used in rendered operands), architecture,
  endianness/processor mode, decoder version and normalisation policy, so any key change
  is a new entry (invalidation by identity; entries are bounded and evictable).

- `score_only()` runs the same comparison core as `diff()` — there is deliberately one
  scorer, so the two paths cannot diverge — but skips building display rows and returns a
  `Comparison` with compact mismatch counts and a structured mismatch summary.
  `render_diff()` materialises the human-facing rows on demand.

- Fast paths are safe: `bytes_match` is the normalised fingerprint only; it never performs
  ROM linking or required literal/data checks. Callers that link the candidate (rom/match.py)
  override the verdict, and the summary's `verification.function_link_match` stays
  "unverified" until a caller supplies a link verdict. A cheap identity check may rule a
  match out, but the candidate is still scored for search.

- Alternative alignment algorithms are version-gated through `register_aligner()`: the
  baseline version cannot be overwritten, and nothing changes ranking without an explicit
  opt-in to a new, benchmarked version (S-07).
"""

from __future__ import annotations

import difflib
import hashlib
import math
import re
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass, field

import capstone

from .elfobj import FunctionObj, Reloc, mask_relocs

PENALTY_STACKDIFF = 1
PENALTY_REGALLOC = 5
PENALTY_OPERAND = 10
PENALTY_REORDER = 60
PENALTY_INSERTION = 100
PENALTY_DELETION = 100

# Per-mismatch penalties, keyed by the structured mismatch summary's count names (P3.4), in the
# canonical schema order: register, stack_offset, operand, insertion, deletion, reorder.
COUNT_PENALTIES = {
    "register": PENALTY_REGALLOC,
    "stack_offset": PENALTY_STACKDIFF,
    "operand": PENALTY_OPERAND,
    "insertion": PENALTY_INSERTION,
    "deletion": PENALTY_DELETION,
    "reorder": PENALTY_REORDER,
}

_CS = {
    ("x86_64", False): (capstone.CS_ARCH_X86, capstone.CS_MODE_64),
    ("x86", False): (capstone.CS_ARCH_X86, capstone.CS_MODE_32),
    # MIPS64 decoding also covers MIPS I/II; N64 (MIPS III) and PS2 (R5900) code uses 64-bit instructions.
    ("mips", True): (capstone.CS_ARCH_MIPS, capstone.CS_MODE_MIPS64 | capstone.CS_MODE_BIG_ENDIAN),
    ("mips", False): (capstone.CS_ARCH_MIPS, capstone.CS_MODE_MIPS64 | capstone.CS_MODE_LITTLE_ENDIAN),
    ("ppc", True): (capstone.CS_ARCH_PPC, capstone.CS_MODE_32 | capstone.CS_MODE_BIG_ENDIAN),
    ("arm", False): (capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM),
    ("thumb", False): (capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB),
}

_REG = re.compile(
    r"^\$?(r[a-z0-9]+|e[a-z]{2}|[a-d][lhx]|[sd]il?|[bs]pl?|[xyz]mm\d+|r\d+[dwb]?"  # x86
    r"|zero|at|v[01]|a[0-3]|t\d|s[0-8]|k[01]|gp|fp|ra|f\d+"  # mips
    r"|cr\d|lr|pc|ip|sb|sl|d\d+|s\d+)$"  # ppc / arm
)
_NUM = re.compile(r"^-?(0x[0-9a-f]+|\d+)$", re.I)
_STACK = re.compile(r"\b(rsp|rbp|esp|ebp|\$?sp|\$?fp|r1)\b")
_TOKEN = re.compile(r"[\w.$<>+@-]+|\S")

# Versioned preparation components (P3.1 cache key; S-06).
NORMALISATION_POLICY = "normalise-v1"  # reloc symbolization rules below
DECODER_VERSION = f"capstone-{capstone.__version__}"
ALIGNMENT_VERSION = "sequence-v1"  # baseline SequenceMatcher alignment (S-07 gate)
SUMMARY_SCHEMA_VERSION = 1  # P3.4 structured mismatch summary
_PREPARE_CACHE_LIMIT = 512


@dataclass
class Insn:
    offset: int
    mnemonic: str
    operands: str
    text: str  # normalized "mnemonic operands" with reloc targets symbolized
    size: int = 0  # encoded width in bytes (variable-width ISAs)
    raw: bytes = b""  # the decoded bytes (of the relocation-masked code)
    relocs: tuple[Reloc, ...] = ()  # every relocation covering this instruction


def percent_text(p: float | None) -> str:
    """A match percentage that never reads 100% before the function really matches: 99.96 is 99.9%, not 100.0%."""
    if p is None:
        return "?"
    if p >= 100:
        return "100%"
    return f"{math.floor(max(0.0, p) * 10) / 10:.1f}%"


# ------------------------------------------------------------------ decoders
# Capstone decoder objects are thread-confined: one per (arch, mode) per thread.
# Sharing a decoder across threads is not attempted (P3.1).

_decoder_local = threading.local()


def _decoder(arch: int, mode: int) -> capstone.Cs:
    cache = getattr(_decoder_local, "cache", None)
    if cache is None:
        cache = _decoder_local.cache = {}
    md = cache.get((arch, mode))
    if md is None:
        md = capstone.Cs(arch, mode)
        md.detail = True
        md.skipdata = True
        cache[(arch, mode)] = md
    return md


def _sext16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def _mips_reloc_names(relocs) -> dict[int, str]:
    """GNU-as style operands for MIPS relocations: %hi(sym+off), %lo(sym+off), plain symbols for jal/j.
    MIPS objects use REL relocations, so each HI16/LO16 pair carries the symbol offset split across the
    two instruction fields; recombine it so both halves show the real offset (e.g. a struct field)."""
    out: dict[int, str] = {}
    for k, r in enumerate(relocs):
        if r.type == 4:  # R_MIPS_26
            out[r.offset] = r.symbol
        elif r.type in (5, 6):  # R_MIPS_HI16 / R_MIPS_LO16
            if r.type == 5:
                pair = next((x for x in relocs[k + 1:] if x.type == 6 and x.symbol == r.symbol), None)
                hi, lo = r.addend, pair.addend if pair else 0
            else:
                pair = next((x for x in reversed(relocs[:k]) if x.type == 5 and x.symbol == r.symbol), None)
                hi, lo = (pair.addend if pair else 0), r.addend
            full = (hi << 16) + _sext16(lo) if max(hi, lo) <= 0xFFFF else r.addend
            off = f"+{full:#x}" if full > 0 else f"-{-full:#x}" if full < 0 else ""
            out[r.offset] = f"%{'hi' if r.type == 5 else 'lo'}({r.symbol}{off})"
        elif r.type == 7:  # R_MIPS_GPREL16
            out[r.offset] = f"%gp_rel({r.symbol})"
    return out


def _mips_symbolize(ops: str, name: str) -> str:
    mem = re.search(r"(-?0x[0-9a-f]+|-?\d+)?\((\$\w+)\)$", ops)
    if name.startswith("%") and mem:  # load/store: offset(base)
        return f"{ops[:mem.start()]}{name}({mem.group(2)})"
    nums = list(re.finditer(r"-?0x[0-9a-f]+|\b\d+\b", ops))
    if nums:
        return ops[: nums[-1].start()] + name + ops[nums[-1].end():]
    return f"{ops}, {name}" if ops else name


def _symbolize(ins, ops: str, rel_at: int, target: str, arch: str) -> str:
    """Put a relocation's symbol into the operand it actually patches (the field is zeroed)."""
    if arch in ("x86_64", "x86"):
        if ins.disp_size and ins.disp_offset == rel_at and "[" in ops:
            lb, rb = ops.index("["), ops.index("]")
            inner = re.sub(r"\s*[+-]\s*(0x[0-9a-f]+|\d+)$", "", ops[lb + 1 : rb])
            return f"{ops[:lb + 1]}{inner} + {target}{ops[rb:]}"
        if ins.imm_size and ins.imm_offset == rel_at:
            outside = re.sub(r"\[.*?\]", lambda m: " " * len(m.group()), ops)
            nums = list(re.finditer(r"-?0x[0-9a-f]+|\b\d+\b", outside))
            return ops[: nums[-1].start()] + target + ops[nums[-1].end() :] if nums else f"{ops} {target}"
    nums = list(re.finditer(r"-?0x[0-9a-f]+|\b\d+\b", ops))
    return ops[: nums[-1].start()] + target + ops[nums[-1].end() :] if nums else f"{ops} {target}".strip()


def _normalise_v1(ins, ops: str, covering: list[Reloc], mips_names: dict[int, str], arch: str) -> str:
    """Baseline operand normalization: the first relocation covering the instruction is symbolized into
    its operand (MIPS %hi/%lo/name forms, x86 disp/imm forms); otherwise intra-function branches become
    position-relative labels. `covering` preserves the relocation list order."""
    rel = covering[0] if covering else None
    if rel is not None and rel.offset in mips_names:
        return _mips_symbolize(ops, mips_names[rel.offset])
    if rel is not None:
        off = rel.addend
        if arch == "x86_64" and rel.type in (2, 4):
            # PC-relative: addend = field_offset - (bytes from reloc field to end of insn)
            off += (ins.address + ins.size) - rel.offset
        target = f"<{rel.symbol}{f'{off:+#x}' if off else ''}>"
        return _symbolize(ins, ops, rel.offset - ins.address, target, arch)
    if ins.id and ins.group(capstone.CS_GRP_JUMP) and _NUM.match(ops.strip()):
        return f".L{int(ops, 16):x}"  # intra-function branch: keep it position-relative
    return ops


_NORMALISERS = {NORMALISATION_POLICY: _normalise_v1}
_VERSION_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def register_normalisation_policy(name: str, fn) -> None:
    """Version-gated normalisation policy (S-06/S-07 pattern): the baseline cannot be overwritten,
    and unknown policies are rejected rather than silently serving unversioned output."""
    if not _VERSION_NAME.match(name):
        raise ValueError(f"normalisation policy name must match {_VERSION_NAME.pattern}")
    if name in _NORMALISERS:
        raise ValueError(f"normalisation policy {name!r} is already registered")
    _NORMALISERS[name] = fn


# ------------------------------------------------------------- prepared targets

@dataclass
class PreparedTarget:
    """Everything the scorer reads from the target side, decoded once (P3.1)."""
    name: str
    arch: str
    big_endian: bool
    code: bytes
    relocs: tuple[Reloc, ...]
    insns: tuple[Insn, ...]
    reloc_index: dict[int, tuple[Reloc, ...]]  # instruction index -> relocations covering it
    mnemonic_sequence: tuple[str, ...]
    mnemonic_counts: Counter
    normalised_fingerprint: str  # masked bytes + symbolic relocs: the match identity
    exact_digest: str  # sha256 of the raw code bytes: exact-byte identity, kept distinct
    policy: str
    decoder_version: str

    @property
    def n_insns(self) -> int:
        return len(self.insns)

    def relocs_at(self, offset: int) -> tuple[Reloc, ...]:
        """Relocations covering the instruction that contains `offset` (empty tuple if none)."""
        lo, hi = 0, len(self.insns)
        while lo < hi:
            mid = (lo + hi) // 2
            ins = self.insns[mid]
            if offset < ins.offset:
                hi = mid
            elif offset >= ins.offset + ins.size:
                lo = mid + 1
            else:
                return self.reloc_index.get(mid, ())
        return ()


@dataclass(frozen=True)
class _PrepareKey:
    arch: str
    big_endian: bool
    code_digest: str
    relocs: tuple  # every relocation's (offset, type, symbol, addend): symbol-resolution data
    policy: str
    decoder_version: str


_prepare_cache: OrderedDict[_PrepareKey, PreparedTarget] = OrderedDict()
_prepare_lock = threading.Lock()
_prepare_stats = {"hits": 0, "misses": 0, "evictions": 0}


def prepare(fn: FunctionObj, policy: str | None = None) -> PreparedTarget:
    """Decode and normalise a function once, cached by content and every material input. A change in
    bytes, relocations (symbol-resolution data), architecture, endianness/mode, decoder version or
    normalisation policy is a different key, so changed inputs can never reuse a stale preparation."""
    policy = NORMALISATION_POLICY if policy is None else policy
    normaliser = _NORMALISERS.get(policy)
    if normaliser is None:
        raise ValueError(f"unknown normalisation policy {policy!r} "
                         f"(registered: {', '.join(sorted(_NORMALISERS))})")
    key = _PrepareKey(fn.arch, fn.big_endian, hashlib.sha256(fn.code).hexdigest(),
                      tuple(r.key() for r in fn.relocs), policy, DECODER_VERSION)
    with _prepare_lock:
        cached = _prepare_cache.get(key)
        if cached is not None:
            _prepare_cache.move_to_end(key)
            _prepare_stats["hits"] += 1
            return cached
    target = _prepare_uncached(fn, policy, normaliser)
    with _prepare_lock:
        existing = _prepare_cache.get(key)
        if existing is not None:  # another thread finished the same key first
            _prepare_stats["hits"] += 1
            return existing
        _prepare_cache[key] = target
        _prepare_stats["misses"] += 1
        while len(_prepare_cache) > _PREPARE_CACHE_LIMIT:
            _prepare_cache.popitem(last=False)
            _prepare_stats["evictions"] += 1
    return target


def _prepare_uncached(fn: FunctionObj, policy: str, normaliser) -> PreparedTarget:
    arch, mode = _CS[(fn.arch, fn.big_endian)]
    md = _decoder(arch, mode)
    code = mask_relocs(fn.code, fn.relocs, fn.arch, fn.big_endian)
    mips_names = _mips_reloc_names(fn.relocs) if fn.arch == "mips" else {}
    insns: list[Insn] = []
    reloc_index: dict[int, tuple[Reloc, ...]] = {}
    for ins in md.disasm(code, 0):
        # Every relocation covering this instruction, in relocation-list order (the first one wins
        # symbolization, exactly as the baseline diff did; the rest stay available in the index).
        covering = tuple(r for r in fn.relocs if ins.address <= r.offset < ins.address + ins.size)
        ops = normaliser(ins, ins.op_str, list(covering), mips_names, fn.arch)
        insns.append(Insn(ins.address, ins.mnemonic, ops, f"{ins.mnemonic} {ops}".strip(),
                          ins.size, ins.bytes, covering))
        if covering:
            reloc_index[len(insns) - 1] = covering
    return PreparedTarget(
        name=fn.name, arch=fn.arch, big_endian=fn.big_endian, code=fn.code, relocs=tuple(fn.relocs),
        insns=tuple(insns), reloc_index=reloc_index,
        mnemonic_sequence=tuple(i.mnemonic for i in insns), mnemonic_counts=Counter(i.mnemonic for i in insns),
        normalised_fingerprint=fn.fingerprint(), exact_digest=hashlib.sha256(fn.code).hexdigest(),
        policy=policy, decoder_version=DECODER_VERSION,
    )


def prepare_cache_clear() -> None:
    """Drop every prepared target (tests, and explicit invalidation after a decoder/profile swap)."""
    with _prepare_lock:
        _prepare_cache.clear()


def prepare_cache_info() -> dict:
    with _prepare_lock:
        return {"entries": len(_prepare_cache), "limit": _PREPARE_CACHE_LIMIT, **_prepare_stats.copy()}


def disassemble(fn: FunctionObj) -> list[Insn]:
    """The function's normalized instructions (prepared and cached). The list is fresh; the Insn
    objects are shared with the cache — treat them as read-only."""
    return list(prepare(fn).insns)


def asm_text(fn: FunctionObj) -> str:
    return "\n".join(f"{i.offset:5x}:  {i.text}" for i in disassemble(fn))


def _classify(a: Insn, b: Insn) -> tuple[int, str]:
    if a.text == b.text:
        return 0, " "
    ta, tb = _TOKEN.findall(a.operands), _TOKEN.findall(b.operands)
    if len(ta) == len(tb):
        diffs = [(x, y) for x, y in zip(ta, tb) if x != y]
        if all(_REG.match(x) and _REG.match(y) for x, y in diffs):
            return PENALTY_REGALLOC, "r"
        if all(_NUM.match(x) and _NUM.match(y) for x, y in diffs) and _STACK.search(a.operands):
            return PENALTY_STACKDIFF, "s"
    return PENALTY_OPERAND, "i"


# -------------------------------------------------------------- alignment core
# The comparison core is shared by diff() and score_only(): one scorer, two consumers (P3.2).


def _sequence_opcodes_v1(t_ins, c_ins):
    """Baseline alignment: difflib over mnemonic sequences. The versioned unit behind S-07."""
    sm = difflib.SequenceMatcher(a=[i.mnemonic for i in t_ins], b=[i.mnemonic for i in c_ins], autojunk=False)
    return sm.get_opcodes()


_ALIGNERS = {ALIGNMENT_VERSION: _sequence_opcodes_v1}


def register_aligner(version: str, fn) -> None:
    """Version-gated alignment (S-07): an alternative alignment algorithm may only exist under a new
    version name; the baseline is never overwritten, so ranking cannot change silently."""
    if not _VERSION_NAME.match(version):
        raise ValueError(f"alignment version name must match {_VERSION_NAME.pattern}")
    if version in _ALIGNERS:
        raise ValueError(f"alignment version {version!r} is already registered")
    _ALIGNERS[version] = fn


def _resolve_aligner(version: str | None):
    version = ALIGNMENT_VERSION if version is None else version
    fn = _ALIGNERS.get(version)
    if fn is None:
        raise ValueError(f"unknown alignment version {version!r} "
                         f"(registered: {', '.join(sorted(_ALIGNERS))})")
    return fn


def _compare(t_ins, c_ins, *, want_rows: bool, opcodes) -> tuple[int, int, list | None, dict]:
    """The single scoring core: (score, max_score, rows-or-None, counts). score is the pre-verdict
    alignment penalty; callers apply `_finalise` for the bytes_match rule. `counts` uses the P3.4
    summary keys and reconciles exactly with the penalties in COUNT_PENALTIES."""
    max_score = max(1, len(t_ins)) * PENALTY_INSERTION
    rows: list | None = [] if want_rows else None
    score = 0
    missing: Counter[str] = Counter()
    extra: Counter[str] = Counter()
    counts = {"register": 0, "stack_offset": 0, "operand": 0, "insertion": 0, "deletion": 0, "reorder": 0}
    for op, a0, a1, b0, b1 in opcodes:
        if op == "equal":
            for a, b in zip(t_ins[a0:a1], c_ins[b0:b1]):
                pen, mark = _classify(a, b)
                score += pen
                if mark == "r":
                    counts["register"] += 1
                elif mark == "s":
                    counts["stack_offset"] += 1
                elif mark == "i":
                    counts["operand"] += 1
                if rows is not None:
                    rows.append((mark, a.text, b.text))
            continue
        ta, cb = t_ins[a0:a1], c_ins[b0:b1]
        for k in range(max(len(ta), len(cb))):
            a = ta[k] if k < len(ta) else None
            b = cb[k] if k < len(cb) else None
            if a and b:
                if rows is not None:
                    rows.append(("|", a.text, b.text))
                missing[a.text] += 1
                extra[b.text] += 1
            elif a:
                if rows is not None:
                    rows.append(("<", a.text, ""))
                missing[a.text] += 1
            else:
                if rows is not None:
                    rows.append((">", "", b.text))
                extra[b.text] += 1
    # instructions that are merely in the wrong place are cheaper than real insert/delete
    reordered = sum((missing & extra).values())
    counts["reorder"] = reordered
    counts["deletion"] = sum(missing.values()) - reordered
    counts["insertion"] = sum(extra.values()) - reordered
    score += reordered * PENALTY_REORDER
    score += (sum(missing.values()) - reordered) * PENALTY_DELETION
    score += (sum(extra.values()) - reordered) * PENALTY_INSERTION
    if reordered and rows is not None:
        moved = missing & extra
        rows[:] = [("o" if m in "|<>" and ((t and moved[t]) or (c and moved[c])) else m, t, c) for m, t, c in rows]
    return score, max_score, rows, counts


def _finalise(score: int, bytes_match: bool) -> int:
    """The baseline verdict rule: a bytes match is score 0; a textually identical but byte-different
    comparison is never a clean 0."""
    if bytes_match:
        return 0
    return 1 if score == 0 else score


@dataclass
class DiffResult:
    score: int
    max_score: int
    rows: list[tuple[str, str, str]]  # (marker, target, candidate)
    bytes_match: bool

    @property
    def percent(self) -> float:
        if self.bytes_match:
            return 100.0
        return max(0.0, 100.0 * (1 - self.score / self.max_score)) if self.max_score else 0.0

    def render(self, context: int = 2, max_rows: int = 60) -> str:
        """Compact side-by-side diff: a count of each kind of mismatch, then only mismatches with a little context.
        Long runs of missing or extra instructions are shortened (a half-written function would otherwise spend
        every row on code that isn't there yet), so each compile costs the agent little context."""
        return render_rows(self.rows, context, max_rows)


MARKERS = {"<": "missing", ">": "extra", "|": "different", "o": "reordered", "r": "regalloc", "s": "stack",
           "i": "operand"}
LEGEND = ("markers: r=regalloc s=stack-offset i=operand |=different insn <=missing (target only) >=extra (yours only) "
          "o=reordered")


def render_rows(rows: list[tuple[str, str, str]], context: int = 2, max_rows: int = 60, run: int = 6,
                legend: str = LEGEND) -> str:
    counts = Counter(m for m, _, _ in rows if m != " ")
    summary = ", ".join(f"{n} {MARKERS.get(m, m)}" for m, n in counts.most_common())
    width = min(max((len(t) for _, t, _ in rows), default=10) + 2, 44)
    show = set()
    for i, (m, _, _) in enumerate(rows):
        if m != " ":
            show.update(range(max(0, i - context), min(len(rows), i + context + 1)))
    lines = [f"   {'TARGET':<{width}}| YOURS"]
    order = sorted(show)
    prev, k = -1, 0
    while k < len(order):
        i = order[k]
        if i != prev + 1:
            lines.append("   ...")
        m = rows[i][0]
        j = i
        while m in "<>" and j + 1 < len(rows) and rows[j + 1][0] == m:
            j += 1
        if j - i + 1 > run:  # a long run of one kind: its start and end, and how much is in between
            for a in (i, i + 1):
                lines.append(f" {m} {rows[a][1][:width - 1]:<{width}}| {rows[a][2]}")
            lines.append(f"   ... {j - i - 2} more {MARKERS[m]} ({'target only' if m == '<' else 'yours only'}) ...")
            lines.append(f" {m} {rows[j][1][:width - 1]:<{width}}| {rows[j][2]}")
            prev = j
            while k < len(order) and order[k] <= j:
                k += 1
        else:
            m, t, c = rows[i]
            lines.append(f" {m} {t[:width - 1]:<{width}}| {c}")
            prev = i
            k += 1
        if len(lines) >= max_rows and k < len(order):
            lines.append(f"   ... ({len(rows) - prev - 1} more rows not shown: fix these first)")
            break
    else:
        if prev != len(rows) - 1:
            lines.append("   ...")
    return (f"{summary}\n" if summary else "") + legend + "\n" + "\n".join(lines)


_SPLAT_PREFIX = re.compile(r"^/\*\s*[0-9A-Fa-f]+\s+[0-9A-Fa-f]{8}\s+[0-9A-Fa-f]{8}\s*\*/\s*")


def compact_asm(text: str) -> str:
    """splat's listing without the per-line `/* offset address bytes */` comment and alignment padding: about half
    the size, so an agent's context holds more of the work. Labels (.L80012345) still carry the addresses."""
    out = []
    for line in text.splitlines():
        m = _SPLAT_PREFIX.match(line)
        if m:
            op, _, args = line[m.end():].strip().partition(" ")
            line = f"  {op} {args.strip()}" if args.strip() else f"  {op}"
            line = re.sub(r",\s+", ", ", line)
        out.append(line)
    return "\n".join(out)


# --------------------------------------------------- structured mismatch summary

_COVERAGE_STATUSES = ("complete", "prefix_limited", "unknown")
_LINK_STATUSES = ("exact", "mismatch", "unverified")


def mismatch_summary(counts: dict, *, bytes_match: bool, score: int,
                     coverage: str = "complete", function_link_match: str = "unverified",
                     observations: list[str] | None = None, hypotheses: list[str] | None = None) -> dict:
    """The P3.4 structured mismatch summary. Only facts the comparison can derive are emitted:
    observations are never "control flow is identical" — that requires analysis, not mnemonic
    alignment. The link verdict is "unverified" unless a caller that actually linked the candidate
    supplies it (scoring alone never links)."""
    if coverage not in _COVERAGE_STATUSES:
        raise ValueError(f"coverage status must be one of {_COVERAGE_STATUSES}")
    if function_link_match not in _LINK_STATUSES:
        raise ValueError(f"function_link_match must be one of {_LINK_STATUSES}")
    obs: list[str] = []
    if bytes_match:
        obs.append("normalised bytes and relocation layout match exactly (relocation-normalised exact match)")
    elif all(counts.get(k, 0) == 0 for k in COUNT_PENALTIES):
        obs.append("all aligned instructions read identically, but the normalised bytes or relocation layout "
                   "differ: the encodings, relocation fields or addends differ — not an exact match")
    obs.extend(observations or [])
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "counts": {k: counts.get(k, 0) for k in COUNT_PENALTIES},
        "observations": obs,
        "hypotheses": list(hypotheses or []),
        "coverage": {"status": coverage},
        "verification": {"function_link_match": function_link_match},
    }


@dataclass
class Comparison:
    """The score_only result: numeric score, compact mismatch counts and verification metadata, with the
    display rows deferred. Rendering re-runs the same core over the same prepared inputs, so it can never
    disagree with the score (S-02)."""
    score: int
    max_score: int
    bytes_match: bool
    counts: dict
    summary: dict
    target: PreparedTarget
    candidate: PreparedTarget
    alignment_version: str
    _rows: list | None = field(default=None, repr=False)

    @property
    def percent(self) -> float:
        if self.bytes_match:
            return 100.0
        return max(0.0, 100.0 * (1 - self.score / self.max_score)) if self.max_score else 0.0

    def rows(self) -> list[tuple[str, str, str]]:
        """The display rows, built on first demand by re-running the shared core (same inputs, same
        aligner: the result is the score's exact row form)."""
        if self._rows is None:
            opcodes = _resolve_aligner(self.alignment_version)(self.target.insns, self.candidate.insns)
            score, max_score, rows, _ = _compare(self.target.insns, self.candidate.insns, want_rows=True,
                                                 opcodes=opcodes)
            if rows is None or _finalise(score, self.bytes_match) != self.score or max_score != self.max_score:
                raise RuntimeError("comparison inputs changed after score_only; re-score instead of rendering")
            self._rows = rows
        return self._rows

    def render(self, context: int = 2, max_rows: int = 60) -> str:
        return render_rows(self.rows(), context, max_rows)

    def to_diff_result(self) -> DiffResult:
        return DiffResult(self.score, self.max_score, self.rows(), self.bytes_match)


def render_diff(result: DiffResult | Comparison, context: int = 2, max_rows: int = 60) -> str:
    """Render rows on demand from either a DiffResult or a score-only Comparison (P3.2)."""
    return result.render(context, max_rows)


def score_only(target: FunctionObj, candidate: FunctionObj, *, policy: str | None = None,
               alignment_version: str | None = None) -> Comparison:
    """Numeric score, compact mismatch counts and verification metadata — no display rows (P3.2/P3.4).
    The scoring algorithm is exactly diff()'s; the target side comes from the prepared cache."""
    t = prepare(target, policy)
    c = prepare(candidate, policy)
    version = ALIGNMENT_VERSION if alignment_version is None else alignment_version
    opcodes = _resolve_aligner(version)(t.insns, c.insns)
    score, max_score, _, counts = _compare(t.insns, c.insns, want_rows=False, opcodes=opcodes)
    bytes_match = t.normalised_fingerprint == c.normalised_fingerprint
    final = _finalise(score, bytes_match)
    return Comparison(score=final, max_score=max_score, bytes_match=bytes_match,
                      counts=counts, summary=mismatch_summary(counts, bytes_match=bytes_match, score=final),
                      target=t, candidate=c, alignment_version=version)


def diff(target: FunctionObj, candidate: FunctionObj, *, policy: str | None = None,
         alignment_version: str | None = None) -> DiffResult:
    """The public diff: identical behavior to the baseline (score, rows, bytes_match), now driven by
    prepared targets and the shared comparison core. Golden parity: tests S-01/S-02."""
    t = prepare(target, policy)
    c = prepare(candidate, policy)
    version = ALIGNMENT_VERSION if alignment_version is None else alignment_version
    opcodes = _resolve_aligner(version)(t.insns, c.insns)
    score, max_score, rows, _ = _compare(t.insns, c.insns, want_rows=True, opcodes=opcodes)
    bytes_match = t.normalised_fingerprint == c.normalised_fingerprint
    return DiffResult(score=_finalise(score, bytes_match), max_score=max_score, rows=rows, bytes_match=bytes_match)
