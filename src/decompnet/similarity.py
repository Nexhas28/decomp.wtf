"""Find functions that look alike: same instructions in a similar order.

The idea behind coddog, which decomp communities use to spot copy-pasted code, shared inlined helpers and
template instantiations. A matched look-alike is the best starting point there is: its C often needs only
small changes. Comparison is on mnemonic sequences (operands differ between copies), pre-filtered by length
and instruction mix so that ranking a whole game stays fast.

Plan Phase 6 (PR-20/PR-21):

* ``Features`` captures cheap whole-function features once per function — the mnemonic histogram, its norm,
  the instruction count, and structural facts (basic blocks, branch/loop patterns, operand categories and
  widths, selected exact constants, call/reference patterns, stack-frame touches). It is built lazily and
  cached by ``FeatureIndex`` keyed on ``(function id, revision, target digest, architecture, mode)``, so a
  repeated query against an unchanged corpus rebuilds nothing (R-01) and a split, target revision or mode
  change invalidates just the affected entry (R-02).
* ``FeatureIndex`` partitions entries by compatible architecture/mode and a broad length bucket, keeps an
  inverted index over mnemonics for the coarse pass, and exposes a version and statistics so stale indexes
  are diagnosable (P6.2).
* The sequence comparison is no longer prefix-limited: functions up to ``MAX_OPS`` instructions are compared
  whole; longer functions use their whole-function summary plus bounded anchored-window comparisons (head,
  middle, tail), so two long functions that share their first 800 instructions but differ later are
  distinguished (R-03, plan D09/P6.3).
* ``search`` reranks a shortlist by usability: compiler-profile compatibility, source language, available
  context, relocation layout, verification history and known adaptation requirements, all returned visibly
  on each ``Match`` (P6.4, R-06). Retrieval never updates match state: that stays with the judge (P6.5).
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

FEATURE_VERSION = 2     # feature policy version; an index built under an older policy is diagnosably stale
MAX_OPS = 1200          # compare whole sequences up to this many instructions on either side
WINDOW = 240            # width of the anchored windows used to compare longer functions
WINDOW_FRACTIONS = (0.0, 0.5, 1.0)  # head, middle, tail — the tail keeps long-function evidence whole
LENGTH_BUCKETS = (16, 64, 256, 1024, 4096)  # broad size ranges: bucket i covers [previous bound, this bound)

# ------------------------------------------------------------------ raw extraction

def mips_mnemonics(code: bytes, big_endian: bool = True, vram: int = 0) -> list[str]:
    import rabbitizer

    order = "big" if big_endian else "little"
    out = []
    for i in range(0, len(code) - 3, 4):
        word = int.from_bytes(code[i:i + 4], order)
        out.append(rabbitizer.Instruction(word, vram + i).getOpcodeName())
    return out


def thumb_mnemonics(code: bytes, vram: int = 0) -> list[str]:
    import capstone

    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    md.skipdata = True
    return [ins.mnemonic for ins in md.disasm(code, vram)]


def mnemonics(lines: list[str]) -> list[str]:
    """Mnemonics from formatted instructions ("lwz r3, 0x10(r31)" -> "lwz")."""
    return [l.split(None, 1)[0].lower() for l in lines if l.strip()]


@dataclass(frozen=True)
class Obs:
    """One decoded instruction, with just enough operand detail for structural features."""

    mnemonic: str
    operands: str = ""
    target: int | None = None   # branch target address, when known
    imm: int | None = None      # immediate value, when known
    call: bool = False
    deref: bool = False         # dereferences memory


def mips_pairs(code: bytes, big_endian: bool = True, vram: int = 0) -> list[Obs]:
    """Decode MIPS instructions with operand detail (rabbitizer)."""
    import rabbitizer

    order = "big" if big_endian else "little"
    out = []
    for i in range(0, len(code) - 3, 4):
        word = int.from_bytes(code[i:i + 4], order)
        ins = rabbitizer.Instruction(word, vram + i)
        name = ins.getOpcodeName()
        try:
            text = ins.disassemble()
            operands = text[len(name):].strip() if text.startswith(name) else ""
        except Exception:
            operands = ""
        target = imm = None
        try:
            target = ins.getBranchVramGeneric()
        except Exception:
            pass
        try:
            imm = ins.getImmediate()
            if imm is not None and 0x8000 <= imm <= 0xFFFF:
                imm -= 0x10000  # the 16-bit immediate field, sign-extended
        except Exception:
            pass
        out.append(Obs(name, operands, target=target, imm=imm,
                       call=ins.isFunctionCall(), deref=ins.doesDereference()))
    return out


def thumb_pairs(code: bytes, vram: int = 0) -> list[Obs]:
    """Decode Thumb instructions with operand detail (capstone)."""
    import capstone

    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    md.skipdata = True
    out = []
    for ins in md.disasm(code, vram):
        try:
            groups = ins.groups
            ops = list(ins.operands)
        except Exception:
            groups, ops = (), []
        target = imm = None
        if capstone.CS_GRP_JUMP in groups or capstone.CS_GRP_CALL in groups:
            for op in ops:
                if op.type == capstone.arm.ARM_OP_IMM:
                    target = op.imm
                    break
        for op in ops:
            if op.type == capstone.arm.ARM_OP_IMM:
                imm = op.imm
                break
        out.append(Obs(ins.mnemonic, ins.op_str, target=target, imm=imm,
                       call=capstone.CS_GRP_CALL in groups,
                       deref=any(op.type == capstone.arm.ARM_OP_MEM for op in ops)))
    return out


def disassemble(data: bytes, arch: str, mode: str, addr: int = 0) -> list[Obs]:
    """The single entry point FeatureIndex uses to decode bytes (tests count calls through it)."""
    if arch == "mips":
        return mips_pairs(data, big_endian=mode != "little", vram=addr)
    if arch in ("arm", "thumb"):
        return thumb_pairs(data, vram=addr)
    raise ValueError(f"no disassembler for arch {arch!r}")


# ------------------------------------------------------------------ feature model

_REG = re.compile(r"^(r\d{1,2}|f\d{1,2}|x\d{1,2}|w\d{1,2}|s\d{1,2}|d\d{1,2}|v\d|a\d|t\d|k[01]|q\d|"
                  r"zero|at|gp|sp|fp|ra|lr|pc|ip|cr\d+)$", re.I)
_HEXNUM = re.compile(r"^-?(?:0x[0-9a-fA-F]+|\d+)$")
_FLOAT = re.compile(r"^-?\d+\.\d+$")
_PCT = re.compile(r"%\w+\(([^)]*)\)")  # %hi(x), %lo(x) etc. refer to the name inside
_BRANCHES = {"b", "beq", "bne", "blt", "bgt", "ble", "bge", "bltz", "bgez", "bgtz", "blez", "beqz",
             "bnez", "bc", "bc1t", "bc1f", "j", "jr", "bra", "bcc", "bcs", "blo", "bhs", "cbz", "cbnz",
             "tbnz", "tbz"}
_CALLS = {"jal", "jals", "bal", "bl", "blx", "call", "jsr", "bsr"}
_SP_REGS = {"sp", "r1", "r31", "x31"}
# Access widths of the common load/store mnemonics, in bits (structural "operand width" evidence).
_ACCESS_WIDTH = {
    "lb": 8, "lbu": 8, "lbz": 8, "sb": 8, "stb": 8, "ldrb": 8, "strb": 8,
    "lh": 16, "lhu": 16, "lhz": 16, "sh": 16, "sth": 16, "ldrh": 16, "strh": 16,
    "lw": 32, "lwu": 32, "lwz": 32, "sw": 32, "stw": 32, "ldr": 32, "str": 32,
    "ld": 64, "sd": 64, "ldrd": 64, "strd": 64, "lq": 128, "sq": 128,
}
_SENTINELS = {-1, 0xFFFF, 0xFFFFFFFF, 0x7FFFFFFF, 0x80000000, 0x8000000000000000}
_MAX_CONSTANTS = 16  # cap of exact constants kept per function


def _parse_operands(m: str, operands: str, is_branch: bool) -> tuple[Counter, int | None, list[int], list[str], bool]:
    """(categories, width, constants, referents, stack-touch) from one instruction's operand text."""
    cats: Counter = Counter()
    width = _ACCESS_WIDTH.get(m)
    consts: list[int] = []
    refs: list[str] = []
    stack = False
    for raw in re.split(r"[,\s]+", operands.strip()):
        if not raw:
            continue
        pct = _PCT.fullmatch(raw)
        if pct:  # %hi(label) etc.: a reference, not a constant
            refs.append(pct.group(1))
            cats["label"] += 1
            continue
        mem = "(" in raw or ")" in raw
        if mem:
            cats["mem"] += 1
        for token in (t.lstrip("$") for t in re.split(r"[()]", raw) if t):
            sign = -1 if token.startswith("-") else 1
            body = token.lstrip("+-")
            if _HEXNUM.fullmatch(token):
                value = sign * int(body, 16 if body.lower().startswith("0x") else 10)
                cats["imm"] += 1
                if not is_branch and abs(value) >= 32 and value not in _SENTINELS:
                    consts.append(value)
            elif _FLOAT.match(token):
                cats["float"] += 1
            elif _REG.fullmatch(token):
                cats["reg"] += 1
                if token.lower() in _SP_REGS:
                    stack = True
            else:
                cats["label"] += 1
                if m in _CALLS or mem or m.startswith(("ld", "st")):
                    refs.append(token)
    return cats, width, consts, refs, stack


@dataclass(frozen=True)
class Features:
    """Whole-function features, cheap to compute and to compare (P6.2/P6.3)."""

    ops: tuple[str, ...]          # every mnemonic, including the tail of long functions
    hist: dict[str, int]          # mnemonic histogram (whole function)
    norm: float                   # L2 norm of the histogram
    length: int
    blocks: int                   # estimated basic-block count (distinct branch targets + 1)
    branches: int
    loops: int                    # backward branch edges
    calls: int
    stack: int                    # stack-pointer-touching instructions
    cats: dict[str, int]          # operand categories: reg, imm, mem, label, float
    widths: dict[str, int]        # memory access widths (8/16/32/64/128 bits)
    constants: tuple[int, ...]    # selected exact constants (discrimination only; the judge decides)
    referents: tuple[str, ...]    # names the code calls or refers to
    revision: int = 1
    digest: str = ""              # target digest this was built from (cache identity)

    def cosine(self, other: "Features") -> float:
        return _cosine(self.hist, other.hist)

    @classmethod
    def from_ops(cls, ops) -> "Features":
        """Mnemonics only (the legacy ``rank`` path): structural facts derived from mnemonics alone."""
        return cls.from_obs([Obs(o) for o in ops], base=0, width=4)

    @classmethod
    def from_lines(cls, lines: list[str], base: int = 0, width: int = 4) -> "Features":
        """Features from formatted instructions ("lwz r3, 0x10(r31)"): the synthetic test format."""
        obs = []
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            m = parts[0].lower()
            operands = parts[1] if len(parts) > 1 else ""
            target = None
            if m in _BRANCHES | _CALLS:
                for tok in re.split(r"[,\s]+", operands):
                    if _HEXNUM.fullmatch(tok):
                        target = int(tok, 0)
                        break
            obs.append(cls._obs_from_text(m, operands, target))
        return cls.from_obs(obs, base, width)

    @staticmethod
    def _obs_from_text(m: str, operands: str, target: int | None) -> Obs:
        imm = None
        for tok in re.split(r"[,\s]+", operands):
            if _HEXNUM.fullmatch(tok):
                imm = int(tok, 0)
                break
        return Obs(m, operands, target=target, imm=imm, call=m in _CALLS, deref="(" in operands)

    @classmethod
    def from_pairs(cls, pairs, base: int = 0, width: int = 4) -> "Features":
        """Features from (mnemonic, operands) pairs."""
        obs = []
        for i, (m, operands) in enumerate(pairs):
            m = m.lower()
            target = None
            if m in _BRANCHES | _CALLS:
                for tok in re.split(r"[,\s]+", operands):
                    if _HEXNUM.fullmatch(tok):
                        target = int(tok, 0)
                        break
            obs.append(cls._obs_from_text(m, operands, target))
        return cls.from_obs(obs, base, width)

    @classmethod
    def from_obs(cls, obs: list[Obs], base: int = 0, width: int = 4) -> "Features":
        ops = tuple(o.mnemonic for o in obs)
        hist = Counter(ops)
        norm = math.sqrt(sum(v * v for v in hist.values()))
        cats: Counter = Counter()
        widths: Counter = Counter()
        consts: set[int] = set()
        refs: list[str] = []
        branches = loops = calls = stack = 0
        targets: set[int] = set()
        for i, o in enumerate(obs):
            addr = base + i * width
            m = o.mnemonic
            c, w, cs, rs, st = _parse_operands(m, o.operands, o.target is not None)
            cats += c
            if w is not None:
                widths[str(w)] += 1
            consts.update(cs)
            refs += rs
            if o.call:
                calls += 1
            if m in _BRANCHES or o.target is not None:
                branches += 1
                if o.target is not None:
                    targets.add(o.target)
                    if o.target <= addr:
                        loops += 1
            if st:
                stack += 1
            if o.imm is not None and abs(o.imm) >= 32 and o.imm not in _SENTINELS and o.target is None:
                consts.add(o.imm)
        blocks = 1 + len(targets) if targets else 1 + branches
        kept = tuple(sorted(consts)[:_MAX_CONSTANTS])
        seen, refs_out = set(), []
        for r in refs:
            if r not in seen and _REG.fullmatch(r) is None:
                seen.add(r)
                refs_out.append(r)
        return cls(ops, dict(hist), norm, len(ops), blocks, branches, loops, calls, stack,
                   dict(cats), dict(widths), kept, tuple(refs_out))


# ------------------------------------------------------------------ comparison

def _cosine(a, b) -> float:
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def size_bucket(length: int) -> int:
    """The broad size range a function belongs to (P6.2 length buckets)."""
    for i, bound in enumerate(LENGTH_BUCKETS):
        if length < bound:
            return i
    return len(LENGTH_BUCKETS)


def uses_windows(len_a: int, len_b: int) -> bool:
    """Whether comparing sequences of these lengths uses bounded windows instead of the whole sequence."""
    return max(len_a, len_b) > MAX_OPS


def sequence_similarity(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    """0..1 on the whole sequence. Short pairs are compared in full; long pairs use their whole-function
    summary (see Features) plus bounded anchored windows over head, middle and tail — never only a prefix."""
    if not a or not b:
        return 0.0
    if not uses_windows(len(a), len(b)):
        return SequenceMatcher(None, a, b, autojunk=False).ratio()
    parts = []
    for f in WINDOW_FRACTIONS:
        lo, hi = int(f * (len(a) - WINDOW)), int(f * (len(b) - WINDOW))
        parts.append(SequenceMatcher(None, a[lo:lo + WINDOW], b[hi:hi + WINDOW], autojunk=False).ratio())
    return sum(parts) / len(parts)


def structure_similarity(a: Features, b: Features) -> float:
    """0..1 over normalised topology, operand categories/widths and selected constants (P6.3)."""
    if a.length < 4 or b.length < 4:
        return 1.0 if a.length == b.length else 0.0

    def ratio(x: float, y: float) -> float:
        return min(x, y) / max(x, y) if max(x, y) > 0 else 1.0

    def cos(x: dict, y: dict) -> float:
        if not x and not y:
            return 1.0
        return _cosine(x, y) if x and y else 0.0

    if not a.constants and not b.constants:
        consts = 1.0
    else:
        consts = len(set(a.constants) & set(b.constants)) / max(1, len(set(a.constants) | set(b.constants)))
    parts = {
        "blocks": ratio(a.blocks, b.blocks),
        "branch_density": ratio(a.branches / a.length, b.branches / b.length),
        "loops": ratio(a.loops, b.loops),
        "calls": ratio(a.calls, b.calls),
        "stack": ratio(a.stack, b.stack),
        "operands": cos(a.cats, b.cats),
        "widths": cos(a.widths, b.widths),
        "constants": consts,
    }
    return sum(parts.values()) / len(parts)


def structural_rerank_enabled() -> bool:
    """The structural term of the combined score is opt-in (DECOMPNET_STRUCTURAL_RERANK=1): it stays off
    until it improves recall/precision on a held-out corpus (plan P6 exit gate)."""
    return os.environ.get("DECOMPNET_STRUCTURAL_RERANK") == "1"


def _has_operand_data(f: Features) -> bool:
    """Whether operand-derived fields (categories, constants, calls, stack touches) were actually built. Widths
    come from the mnemonic alone, so they do not count: mnemonic-only features have none of these."""
    return bool(f.cats or f.constants or f.calls or f.stack)


def _similarity(a: Features, b: Features) -> float:
    """The combined 0..1 retrieval score (P6.3). Default: 0.6 * sequence + 0.4 * histogram cosine. With the
    structural rerank enabled: 0.55 * sequence + 0.35 * cosine + 0.10 * structure. The structure term is
    dropped (default weights used) when either side has no operand-level data. Weights sum to 1."""
    if not a.length or not b.length:
        return 0.0
    if structural_rerank_enabled() and _has_operand_data(a) and _has_operand_data(b):
        return 0.55 * sequence_similarity(a.ops, b.ops) + 0.35 * a.cosine(b) + 0.10 * structure_similarity(a, b)
    return 0.6 * sequence_similarity(a.ops, b.ops) + 0.4 * a.cosine(b)


def score(a: list[str], b: list[str]) -> float:
    """0..1, 1 = the same instruction sequence (whole-function; long functions via bounded windows)."""
    if not a or not b:
        return 0.0
    return sequence_similarity(tuple(a), tuple(b))


def rank(target: list[str], candidates: dict[str, list[str]], top: int = 5, floor: float = 0.6) -> list[tuple[str, float]]:
    """The `top` candidates most similar to `target`, best first, at least `floor` similar. Whole-function:
    the tails of long functions count. Inputs are mnemonic lists only, so no operand data is available and
    the structural term is never applied here, even with DECOMPNET_STRUCTURAL_RERANK=1 (default weights are
    used). (Engine callers keep this signature; FeatureIndex.search adds usability ranking and provenance.)"""
    if len(target) < 4:  # tiny functions all look alike
        return []
    tf = Features.from_ops(tuple(target))
    rough = []
    for name, ops in candidates.items():
        if not ops or not (0.6 <= len(ops) / len(target) <= 1 / 0.6):
            continue
        cf = Features.from_ops(tuple(ops))
        c = tf.cosine(cf)
        if c >= floor:
            rough.append((c, name, cf))
    rough.sort(key=lambda t: -t[0])
    exact = [(name, _similarity(tf, cf)) for _, name, cf in rough[:150]]
    exact = [(n, s) for n, s in exact if s >= floor]
    exact.sort(key=lambda x: -x[1])
    return exact[:top]


# ------------------------------------------------------------------ usability & provenance (P6.4)

@dataclass
class CandidateInfo:
    """What is known about a candidate beyond its bytes: source usability and provenance (P6.4)."""

    key: object = None
    source_available: bool = False        # accessible matched source exists for the candidate
    verified: bool = False                # its match carries a verification receipt
    verification_history: str = ""        # human summary of verification history
    compiler_profile: str | None = None   # e.g. "gcc-2.7.2-kmc"
    language: str | None = None           # "C" | "C++" | ...
    context_available: bool = True        # its header/context is present
    reloc_compatible: bool | None = None  # None = unknown
    adaptation: str = ""                  # known adaptation requirements
    provenance: str = ""                  # exact-twin | verified match | matched example | ...


@dataclass
class Match:
    """One ranked candidate with why it was retrieved and what adapting it takes (P6.4)."""

    key: object
    similarity: float           # pure retrieval similarity 0..1
    rank_score: float           # usability-adjusted score the shortlist is ordered by
    why: str                    # why it was retrieved + usability notes
    route: str                  # full-sequence | windowed-sequence (which comparison produced it)
    source_available: bool
    verified: bool
    verification_history: str
    compiler_ok: bool | None
    language: str | None
    context_available: bool
    reloc_compatible: bool | None
    adaptation: str
    provenance: str


def _usability(info: CandidateInfo, target: CandidateInfo | None) -> tuple[float, list[str]]:
    """(bonus, notes) for ranking a candidate by immediate reusability (P6.4). A very similar binary
    without accessible compatible source is informative but less immediately reusable."""
    bonus, notes = 0.0, []
    if info.source_available:
        bonus += 0.05
        notes.append("matched source available")
    else:
        notes.append("no accessible source (binary reference only)")
    if info.verified:
        bonus += 0.03
        notes.append("verified match")
    if info.compiler_profile:
        if target and target.compiler_profile and info.compiler_profile == target.compiler_profile:
            bonus += 0.02
            notes.append(f"compiler {info.compiler_profile} matches the target")
        else:
            notes.append(f"compiler {info.compiler_profile}: retargeting may be needed")
    if info.language and target and target.language:
        if info.language == target.language:
            bonus += 0.01
            notes.append(f"{info.language} source matches the target")
        else:
            notes.append(f"{info.language} source for a {target.language} target: adaptation needed")
    if info.reloc_compatible is False:
        bonus -= 0.02
        notes.append("relocation layout differs")
    elif info.reloc_compatible is True:
        notes.append("relocation layout compatible")
    if not info.context_available:
        notes.append("context unavailable")
    if info.adaptation:
        notes.append(f"adaptation: {info.adaptation}")
    return bonus, notes


# ------------------------------------------------------------------ acceptance feedback (P6.5)

_ledger: dict[int, list[dict]] = {}  # project id -> reuse records
_ledger_lock = threading.Lock()


def note_reuse(project_id: int, function_id: int, source_function_id: int | None = None,
               route: str = "unknown", verified: bool = True, reuse_class: str | None = None) -> None:
    """Record that retrieval route `route` produced verified reuse for a function, and that its source is
    available as a matched example. `reuse_class` is the P10.5 acceptance class, when known. Never updates
    match state: the judge owns that (P6.5)."""
    with _ledger_lock:
        _ledger.setdefault(project_id, []).append({
            "function_id": function_id,
            "source_function_id": source_function_id,
            "route": route,
            "verified": bool(verified),
            "reuse_class": reuse_class,
            "at": time.time(),
        })


def reuse_stats(project_id: int | None = None) -> dict:
    """How often each retrieval route produced verified reuse (P6.5 diagnostics)."""
    with _ledger_lock:
        rows = _ledger.get(project_id, []) if project_id is not None else [r for v in _ledger.values() for r in v]
        by_route: Counter = Counter(r["route"] for r in rows)
        return {"routes": dict(by_route), "records": len(rows)}


# ------------------------------------------------------------------ the cached index (P6.2)

class FeatureIndex:
    """Per-function features cached by (function id, revision, target digest, architecture, mode).

    * partitions by compatible architecture/mode and broad length buckets,
    * keeps an inverted index over mnemonics for the coarse pass,
    * counts builds/hits/invalidations and exposes a version and stats, so a stale index (an older
      feature policy, say) is diagnosable rather than silently wrong.
    """

    def __init__(self, disasm=None, project_id: int | None = None):
        self.disasm = disasm            # None = the module-level disassemble (monkeypatchable)
        self.project_id = project_id
        self.version = FEATURE_VERSION
        self._entries: dict[object, tuple] = {}                       # fid -> (revision, digest, Features, (arch, mode))
        self._by_part: dict[tuple, set] = {}                          # (arch, mode, bucket) -> fids
        self._inverted: dict[tuple, set] = {}                         # (arch, mode, mnemonic) -> fids
        self._avail: set = set()                                      # fids with usable matched source (P6.5)
        self._routes: dict[object, str] = {}                          # fid -> route that produced verified reuse
        self.builds = 0
        self.hits = 0
        self.misses = 0
        self.invalidations = 0

    # ---------------------------------------------------------------- lifecycle

    def _check_policy(self) -> None:
        if self.version != FEATURE_VERSION:
            self.invalidate_all()
            self.version = FEATURE_VERSION

    def invalidate_all(self) -> None:
        self.invalidations += len(self._entries)
        self._entries.clear()
        self._by_part.clear()
        self._inverted.clear()
        self._avail.clear()
        self._routes.clear()

    def invalidate(self, fid) -> None:
        """Drop one entry (a split, say): its partition and inverted-index postings go with it (R-02)."""
        entry = self._entries.pop(fid, None)
        if entry is None:
            return
        self.invalidations += 1
        self._unindex(fid, entry[2], entry[3][0], entry[3][1])
        self._avail.discard(fid)
        self._routes.pop(fid, None)

    def _index(self, fid, feats: Features, arch: str, mode: str) -> None:
        self._by_part.setdefault((arch, mode, size_bucket(feats.length)), set()).add(fid)
        for m in feats.hist:
            self._inverted.setdefault((arch, mode, m), set()).add(fid)

    def _unindex(self, fid, feats: Features, arch: str, mode: str) -> None:
        part = self._by_part.get((arch, mode, size_bucket(feats.length)))
        if part is not None:
            part.discard(fid)
            if not part:
                del self._by_part[(arch, mode, size_bucket(feats.length))]
        for m in feats.hist:
            posting = self._inverted.get((arch, mode, m))
            if posting is not None:
                posting.discard(fid)
                if not posting:
                    del self._inverted[(arch, mode, m)]

    # ---------------------------------------------------------------- features

    def features_for(self, fid, revision: int, digest: str, arch: str, mode: str, data,
                     addr: int = 0) -> Features:
        """The cached features of one function. `data` is target bytes, formatted assembly lines or
        (mnemonic, operands) pairs. Built lazily on first use; rebuilt only when the function's
        revision, target digest, architecture or mode changed (R-01/R-02)."""
        self._check_policy()
        entry = self._entries.get(fid)
        if entry is not None and entry[0] == revision and entry[1] == digest and entry[3] == (arch, mode):
            self.hits += 1
            return entry[2]
        if entry is not None:
            self.invalidations += 1
            self._unindex(fid, entry[2], entry[3][0], entry[3][1])
        self.misses += 1
        feats = self._build(data, arch, mode, addr)
        feats = replace(feats, revision=revision, digest=digest)
        self._entries[fid] = (revision, digest, feats, (arch, mode))
        self._index(fid, feats, arch, mode)
        return feats

    def _build(self, data, arch: str, mode: str, addr: int) -> Features:
        self.builds += 1
        if isinstance(data, (bytes, bytearray, memoryview)):
            feats = Features.from_obs((self.disasm or disassemble)(bytes(data), arch, mode, addr),
                                      base=addr, width=2 if arch in ("arm", "thumb") else 4)
        elif isinstance(data, (list, tuple)) and data and isinstance(data[0], str):
            feats = Features.from_lines(list(data), base=addr)
        else:
            feats = Features.from_pairs(list(data), base=addr)
        return feats

    def features(self, fid) -> Features:
        return self._entries[fid][2]

    # ---------------------------------------------------------------- retrieval

    def candidates_for(self, target: Features, arch: str, mode: str, span: int = 1) -> list:
        """Candidate ids in the compatible partitions whose mnemonics overlap the target's (the inverted
        index) and whose lengths are within the broad range."""
        bucket = size_bucket(target.length)
        in_part: set = set()
        for b in range(max(0, bucket - span), min(len(LENGTH_BUCKETS), bucket + span) + 1):
            in_part |= self._by_part.get((arch, mode, b), set())
        postings: set = set()
        for m in target.hist:
            postings |= self._inverted.get((arch, mode, m), set())
        out = []
        for fid in in_part & postings:
            feats = self._entries[fid][2]
            if 0.6 <= feats.length / target.length <= 1 / 0.6:
                out.append(fid)
        return out

    def search(self, target, arch: str, mode: str, top: int = 5, floor: float = 0.6,
               infos: dict | None = None, target_info: CandidateInfo | None = None) -> list[Match]:
        """Rank the indexed functions against `target` (an indexed fid or a Features). Ordering uses the
        usability-adjusted rank_score; each Match carries the pure similarity and why it was retrieved
        (P6.4). Retrieval never updates match state."""
        self._check_policy()
        if isinstance(target, Features):
            tf = target
        else:
            tf = self._entries[target][2]
        infos = infos or {}
        scored: list[tuple] = []
        for fid in self.candidates_for(tf, arch, mode):
            if fid == target:
                continue
            cf = self._entries[fid][2]
            hist = tf.cosine(cf)
            if hist < floor:
                continue
            sim = _similarity(tf, cf)
            if sim < floor:
                continue
            scored.append((fid, sim))
        scored.sort(key=lambda t: -t[1])
        short = scored[:max(3 * top, 15)]
        matches = []
        for fid, sim in short:
            cf = self._entries[fid][2]
            info = infos.get(fid)
            if info is None:
                info = CandidateInfo(key=fid, source_available=fid in self._avail,
                                     provenance=self._routes.get(fid, ""))
            elif fid in self._avail:  # acceptance feedback supplements what the caller knows (P6.5)
                info = replace(info, source_available=True, provenance=info.provenance or "matched example")
            bonus, notes = _usability(info, target_info)
            route = "windowed-sequence" if uses_windows(tf.length, cf.length) else "full-sequence"  # per pair
            why = "; ".join(x for x in [f"mnemonic histogram and {route} (similarity {sim:.2f})",
                                        info.verification_history, *notes] if x)
            matches.append(Match(key=fid, similarity=sim, rank_score=sim + bonus, why=why, route=route,
                                 source_available=info.source_available, verified=info.verified,
                                 verification_history=info.verification_history,
                                 compiler_ok=(info.compiler_profile == target_info.compiler_profile)
                                 if info.compiler_profile and target_info and target_info.compiler_profile else None,
                                 language=info.language, context_available=info.context_available,
                                 reloc_compatible=info.reloc_compatible, adaptation=info.adaptation,
                                 provenance=info.provenance))
        matches.sort(key=lambda m: (-m.rank_score, -m.similarity))
        return matches[:top]

    # ---------------------------------------------------------------- acceptance feedback (P6.5)

    def acceptance(self, fid, source_fid=None, route: str = "retrieval", reuse_class: str | None = None) -> None:
        """After a verified match: mark the entry's source available and record which retrieval route
        produced the reuse (and its P10.5 class, when given). Match state itself is never touched here
        (the judge owns it)."""
        self._avail.add(fid)
        self._routes[fid] = route
        note_reuse(self.project_id or 0, fid, source_fid, route, reuse_class=reuse_class)

    # ---------------------------------------------------------------- diagnostics (P6.2)

    def stats(self) -> dict:
        self._check_policy()
        buckets: Counter = Counter()
        partitions = {}
        for (arch, mode, bucket), fids in sorted(self._by_part.items(), key=lambda kv: str(kv[0])):
            partitions[f"{arch}/{mode}/bucket-{bucket}"] = len(fids)
            buckets[bucket] += len(fids)
        return {
            "version": self.version,
            "feature_version": FEATURE_VERSION,
            "entries": len(self._entries),
            "builds": self.builds,
            "hits": self.hits,
            "misses": self.misses,
            "invalidations": self.invalidations,
            "partitions": partitions,
            "buckets": dict(sorted(buckets.items())),
            "inverted_terms": len(self._inverted),
            "available_sources": len(self._avail),
            "reuse_routes": dict(self._routes),
        }
