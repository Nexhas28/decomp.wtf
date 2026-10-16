"""S-01..S-07 (plan Section 20, Phase 3): prepared targets, score-only/rendered parity, safe fast
paths, indexed relocation lookup, cache invalidation and the versioned-alignment gate.

The vendored baseline below is the pre-PR-12/13 scoring implementation (reviewed commit 575b4a30,
src/decompnet/scoring.py) transcribed verbatim as the parity oracle: the prepared path must agree
with it on every golden pair. All machine code is hand-assembled synthetic bytes (capstone 5.x
decodes every fixture on this host); no compilers or databases are involved.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections import Counter
from dataclasses import dataclass

import capstone
import pytest

from decompnet import scoring
from decompnet.elfobj import FunctionObj, Reloc, mask_relocs
from decompnet.scoring import (ALIGNMENT_VERSION, COUNT_PENALTIES, NORMALISATION_POLICY,
                               PENALTY_OPERAND, diff, disassemble, prepare, prepare_cache_clear,
                               prepare_cache_info, score_only)

# ---------------------------------------------------------------------------
# Vendored baseline (575b4a30 scoring.py) — the parity oracle for S-01/S-02.
# ---------------------------------------------------------------------------

_CS_B = {
    ("x86_64", False): (capstone.CS_ARCH_X86, capstone.CS_MODE_64),
    ("x86", False): (capstone.CS_ARCH_X86, capstone.CS_MODE_32),
    ("mips", True): (capstone.CS_ARCH_MIPS, capstone.CS_MODE_MIPS64 | capstone.CS_MODE_BIG_ENDIAN),
    ("mips", False): (capstone.CS_ARCH_MIPS, capstone.CS_MODE_MIPS64 | capstone.CS_MODE_LITTLE_ENDIAN),
    ("ppc", True): (capstone.CS_ARCH_PPC, capstone.CS_MODE_32 | capstone.CS_MODE_BIG_ENDIAN),
    ("arm", False): (capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM),
    ("thumb", False): (capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB),
}
_REG_B = re.compile(
    r"^\$?(r[a-z0-9]+|e[a-z]{2}|[a-d][lhx]|[sd]il?|[bs]pl?|[xyz]mm\d+|r\d+[dwb]?"
    r"|zero|at|v[01]|a[0-3]|t\d|s[0-8]|k[01]|gp|fp|ra|f\d+"
    r"|cr\d|lr|pc|ip|sb|sl|d\d+|s\d+)$"
)
_NUM_B = re.compile(r"^-?(0x[0-9a-f]+|\d+)$", re.I)
_STACK_B = re.compile(r"\b(rsp|rbp|esp|ebp|\$?sp|\$?fp|r1)\b")
_TOKEN_B = re.compile(r"[\w.$<>+@-]+|\S")
P_STACK, P_REG, P_OP, P_REORD, P_INS, P_DEL = 1, 5, 10, 60, 100, 100


@dataclass
class BInsn:
    offset: int
    mnemonic: str
    operands: str
    text: str


def b_sext16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def b_mips_reloc_names(relocs) -> dict[int, str]:
    out: dict[int, str] = {}
    for k, r in enumerate(relocs):
        if r.type == 4:
            out[r.offset] = r.symbol
        elif r.type in (5, 6):
            if r.type == 5:
                pair = next((x for x in relocs[k + 1:] if x.type == 6 and x.symbol == r.symbol), None)
                hi, lo = r.addend, pair.addend if pair else 0
            else:
                pair = next((x for x in reversed(relocs[:k]) if x.type == 5 and x.symbol == r.symbol), None)
                hi, lo = (pair.addend if pair else 0), r.addend
            full = (hi << 16) + b_sext16(lo) if max(hi, lo) <= 0xFFFF else r.addend
            off = f"+{full:#x}" if full > 0 else f"-{-full:#x}" if full < 0 else ""
            out[r.offset] = f"%{'hi' if r.type == 5 else 'lo'}({r.symbol}{off})"
        elif r.type == 7:
            out[r.offset] = f"%gp_rel({r.symbol})"
    return out


def b_mips_symbolize(ops: str, name: str) -> str:
    mem = re.search(r"(-?0x[0-9a-f]+|-?\d+)?\((\$\w+)\)$", ops)
    if name.startswith("%") and mem:
        return f"{ops[:mem.start()]}{name}({mem.group(2)})"
    nums = list(re.finditer(r"-?0x[0-9a-f]+|\b\d+\b", ops))
    if nums:
        return ops[: nums[-1].start()] + name + ops[nums[-1].end():]
    return f"{ops}, {name}" if ops else name


def b_symbolize(ins, ops: str, rel_at: int, target: str, arch: str) -> str:
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


def b_disassemble(fn: FunctionObj) -> list[BInsn]:
    arch, mode = _CS_B[(fn.arch, fn.big_endian)]
    md = capstone.Cs(arch, mode)
    md.detail = True
    md.skipdata = True
    code = mask_relocs(fn.code, fn.relocs, fn.arch, fn.big_endian)
    mips_names = b_mips_reloc_names(fn.relocs) if fn.arch == "mips" else {}
    out: list[BInsn] = []
    for ins in md.disasm(code, 0):
        ops = ins.op_str
        rel = next((r for r in fn.relocs if ins.address <= r.offset < ins.address + ins.size), None)
        if rel is not None and rel.offset in mips_names:
            ops = b_mips_symbolize(ops, mips_names[rel.offset])
        elif rel is not None:
            off = rel.addend
            if fn.arch == "x86_64" and rel.type in (2, 4):
                off += (ins.address + ins.size) - rel.offset
            target = f"<{rel.symbol}{f'{off:+#x}' if off else ''}>"
            ops = b_symbolize(ins, ops, rel.offset - ins.address, target, fn.arch)
        elif ins.id and ins.group(capstone.CS_GRP_JUMP) and _NUM_B.match(ops.strip()):
            ops = f".L{int(ops, 16):x}"
        out.append(BInsn(ins.address, ins.mnemonic, ops, f"{ins.mnemonic} {ops}".strip()))
    return out


def b_classify(a: BInsn, b: BInsn) -> tuple[int, str]:
    if a.text == b.text:
        return 0, " "
    ta, tb = _TOKEN_B.findall(a.operands), _TOKEN_B.findall(b.operands)
    if len(ta) == len(tb):
        diffs = [(x, y) for x, y in zip(ta, tb) if x != y]
        if all(_REG_B.match(x) and _REG_B.match(y) for x, y in diffs):
            return P_REG, "r"
        if all(_NUM_B.match(x) and _NUM_B.match(y) for x, y in diffs) and _STACK_B.search(a.operands):
            return P_STACK, "s"
    return P_OP, "i"


def b_diff(target: FunctionObj, candidate: FunctionObj) -> tuple[int, int, list, bool]:
    t_ins, c_ins = b_disassemble(target), b_disassemble(candidate)
    max_score = max(1, len(t_ins)) * P_INS
    bytes_match = target.fingerprint() == candidate.fingerprint()

    sm = difflib.SequenceMatcher(a=[i.mnemonic for i in t_ins], b=[i.mnemonic for i in c_ins], autojunk=False)
    rows: list[tuple[str, str, str]] = []
    score = 0
    missing: Counter[str] = Counter()
    extra: Counter[str] = Counter()
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op == "equal":
            for a, b in zip(t_ins[a0:a1], c_ins[b0:b1]):
                pen, mark = b_classify(a, b)
                score += pen
                rows.append((mark, a.text, b.text))
            continue
        ta, cb = t_ins[a0:a1], c_ins[b0:b1]
        for k in range(max(len(ta), len(cb))):
            a = ta[k] if k < len(ta) else None
            b = cb[k] if k < len(cb) else None
            if a and b:
                rows.append(("|", a.text, b.text))
                missing[a.text] += 1
                extra[b.text] += 1
            elif a:
                rows.append(("<", a.text, ""))
                missing[a.text] += 1
            else:
                rows.append((">", "", b.text))
                extra[b.text] += 1
    reordered = sum((missing & extra).values())
    score += reordered * P_REORD
    score += (sum(missing.values()) - reordered) * P_DEL
    score += (sum(extra.values()) - reordered) * P_INS
    if reordered:
        moved = missing & extra
        rows = [("o" if m in "|<>" and ((t and moved[t]) or (c and moved[c])) else m, t, c) for m, t, c in rows]

    if score == 0 and not bytes_match:
        score = 1
    if bytes_match:
        score = 0
    return score, max_score, rows, bytes_match


# ---------------------------------------------------------------------------
# Golden fixtures: hand-assembled machine code per architecture.
# ---------------------------------------------------------------------------

def obj(arch: str, big_endian: bool, code: bytes, relocs=()) -> FunctionObj:
    return FunctionObj("f", arch, big_endian, code, list(relocs))


def be(*words: int) -> bytes:
    return b"".join(w.to_bytes(4, "big") for w in words)


def le(*words: int) -> bytes:
    return b"".join(w.to_bytes(4, "little") for w in words)


X64_BASE = bytes.fromhex("55 48 89 e5 48 8b 45 10 5d c3")
X64_REG = bytes.fromhex("53 48 89 e3 48 8b 43 10 5b c3")
X64_STACK = bytes.fromhex("55 48 89 e5 48 8b 45 18 5d c3")
X64_IMM = bytes.fromhex("b8 01 00 00 00 c3")
X64_IMM2 = bytes.fromhex("b8 02 00 00 00 c3")
X64_INS = bytes.fromhex("90 55 48 89 e5 48 8b 45 10 5d c3")
X64_DEL = bytes.fromhex("55 48 89 e5 48 8b 45 10 5d")
X64_RO_B = bytes.fromhex("55 48 83 ec 08 b8 01 00 00 00 c3")
X64_RO_V = bytes.fromhex("48 83 ec 08 55 b8 01 00 00 00 c3")

X32_BASE = bytes.fromhex("55 89 e5 8b 45 08 5d c3")
X32_REG = bytes.fromhex("53 89 e3 8b 43 08 5b c3")
X32_STACK = bytes.fromhex("55 89 e5 8b 45 0c 5d c3")
X32_INS = bytes.fromhex("90 55 89 e5 8b 45 08 5d c3")
X32_DEL = bytes.fromhex("55 89 e5 8b 45 08 5d")

MIPS_WORDS = (0x27BDFFF8, 0xAFBF0004, 0x0C000000, 0, 0x8FBF0004, 0x03E00008, 0)
MIPS_REG_WORDS = (0x27BDFFF8, 0xAFB00004, 0x0C000000, 0, 0x8FB00004, 0x02000008, 0)
MIPS_INS_WORDS = (0,) + MIPS_WORDS
MIPS_DEL_WORDS = MIPS_WORDS[:-1]
MIPS_BASE, MIPS_REG, MIPS_INS, MIPS_DEL = (be(*w) for w in
                                           (MIPS_WORDS, MIPS_REG_WORDS, MIPS_INS_WORDS, MIPS_DEL_WORDS))
MIPS_BASE_LE, MIPS_REG_LE, MIPS_INS_LE = (le(*w) for w in (MIPS_WORDS, MIPS_REG_WORDS, MIPS_INS_WORDS))
MIPS_RELOC = [Reloc(8, 4, "target", 0)]  # R_MIPS_26 on the jal
MIPS_INS_RELOC = [Reloc(12, 4, "target", 0)]

PPC_WORDS = (0x7C0802A6, 0x90010004, 0x48000001, 0x80010004, 0x7C0803A6, 0x4E800020)
PPC_REG_WORDS = (0x7C6802A6, 0x90610004, 0x48000001, 0x80610004, 0x7C6803A6, 0x4E800020)
PPC_INS_WORDS = (0x60000000,) + PPC_WORDS
PPC_DEL_WORDS = PPC_WORDS[:-1]
PPC_BASE, PPC_REG, PPC_INS, PPC_DEL = (be(*w) for w in (PPC_WORDS, PPC_REG_WORDS, PPC_INS_WORDS, PPC_DEL_WORDS))
PPC_RELOC = [Reloc(8, 10, "func", 0)]  # R_PPC_REL24 on the bl
PPC_INS_RELOC = [Reloc(12, 10, "func", 0)]

ARM_WORDS = (0xE92D4010, 0xE1A04000, 0xE59F0004, 0xE0800004, 0xE8BD8010)
ARM_REG_WORDS = (0xE92D4020, 0xE1A05000, 0xE59F0004, 0xE0800005, 0xE8BD8020)
ARM_INS_WORDS = (0xE1A00000,) + ARM_WORDS
ARM_DEL_WORDS = ARM_WORDS[:-1]
ARM_BASE, ARM_REG, ARM_INS, ARM_DEL = (le(*w) for w in (ARM_WORDS, ARM_REG_WORDS, ARM_INS_WORDS, ARM_DEL_WORDS))

THUMB_BASE = bytes.fromhex("10 b5 04 46 00 f0 01 f8 20 46 10 bd")
THUMB_REG = bytes.fromhex("10 b5 05 46 00 f0 01 f8 28 46 10 bd")
THUMB_INS = bytes.fromhex("c0 46 10 b5 04 46 00 f0 01 f8 20 46 10 bd")
THUMB_DEL = bytes.fromhex("10 b5 04 46 00 f0 01 f8 20 46")
THUMB_RELOC = [Reloc(4, 10, "func", 0)]  # R_ARM_THM_CALL: both halfwords of the 4-byte bl
THUMB_INS_RELOC = [Reloc(6, 10, "func", 0)]


def pairs(arch, big_endian, base, variants, reloc=(), variant_relocs=None):
    """(name, target, candidate) pairs: exact + one per supplied variant."""
    out = [("exact", obj(arch, big_endian, base, reloc), obj(arch, big_endian, base, reloc))]
    for name, code in variants:
        out.append((name, obj(arch, big_endian, base, reloc),
                    obj(arch, big_endian, code, (variant_relocs or {}).get(name, reloc))))
    return out


GOLDEN = [
    ("x86_64", pairs("x86_64", False, X64_BASE,
                     [("regalloc", X64_REG), ("stack", X64_STACK), ("insertion", X64_INS), ("deletion", X64_DEL)])
     + [("operand", obj("x86_64", False, X64_IMM), obj("x86_64", False, X64_IMM2)),
        ("reorder", obj("x86_64", False, X64_RO_B), obj("x86_64", False, X64_RO_V))]),
    ("x86", pairs("x86", False, X32_BASE,
                  [("regalloc", X32_REG), ("stack", X32_STACK), ("insertion", X32_INS), ("deletion", X32_DEL)])),
    ("mips", pairs("mips", True, MIPS_BASE,
                   [("regalloc", MIPS_REG), ("insertion", MIPS_INS), ("deletion", MIPS_DEL)],
                   MIPS_RELOC, {"insertion": MIPS_INS_RELOC})),
    ("mips-le", pairs("mips", False, MIPS_BASE_LE,
                      [("regalloc", MIPS_REG_LE), ("insertion", MIPS_INS_LE)],
                      MIPS_RELOC, {"insertion": MIPS_INS_RELOC})),
    ("ppc", pairs("ppc", True, PPC_BASE,
                  [("regalloc", PPC_REG), ("insertion", PPC_INS), ("deletion", PPC_DEL)],
                  PPC_RELOC, {"insertion": PPC_INS_RELOC})),
    ("arm", pairs("arm", False, ARM_BASE,
                  [("regalloc", ARM_REG), ("insertion", ARM_INS), ("deletion", ARM_DEL)])),
    ("thumb", pairs("thumb", False, THUMB_BASE,
                    [("regalloc", THUMB_REG), ("insertion", THUMB_INS), ("deletion", THUMB_DEL)],
                    THUMB_RELOC, {"insertion": THUMB_INS_RELOC})),
]


def check_relocs_indexed(f: FunctionObj, p) -> None:
    """S-05 invariant: every relocation appears under exactly the instruction whose interval covers it."""
    indexed = Counter()
    for i, ins in enumerate(p.insns):
        for r in p.reloc_index.get(i, ()):
            assert ins.offset <= r.offset < ins.offset + ins.size, (f.name, i, r)
            indexed[r.offset] += 1
    assert sorted(indexed) == sorted(r.offset for r in f.relocs), f.name
    assert sum(indexed.values()) == len(f.relocs), f.name


# --------------------------------------------------------------------------- S-01

@pytest.mark.parametrize("arch,golden", GOLDEN, ids=[a for a, _ in GOLDEN])
def test_s01_prepared_and_baseline_scoring_agree(arch, golden):
    prepare_cache_clear()
    for name, t, c in golden:
        b = b_diff(t, c)
        d = diff(t, c)
        assert (d.score, d.max_score, d.bytes_match) == (b[0], b[1], b[3]), (arch, name)
        assert d.rows == b[2], (arch, name)
        # the prepared disassembly itself agrees with the baseline's per-call disassembly
        assert [i.text for i in disassemble(t)] == [i.text for i in b_disassemble(t)], (arch, name)
        assert [i.text for i in disassemble(c)] == [i.text for i in b_disassemble(c)], (arch, name)
    # prepared targets are actually reused: a second identical diff serves from the cache
    t, c = golden[0][1], golden[0][2]
    info = prepare_cache_info()
    diff(t, c)
    assert prepare_cache_info()["hits"] > info["hits"]


def test_s01_prepared_targets_are_cached_and_identity_kinds_stay_distinct():
    prepare_cache_clear()
    f = obj("mips", True, MIPS_BASE, MIPS_RELOC)
    p1, p2 = prepare(f), prepare(f)
    assert p1 is p2  # one cached object per identity
    assert p1.normalised_fingerprint == f.fingerprint()
    assert p1.exact_digest == hashlib.sha256(f.code).hexdigest()
    assert p1.normalised_fingerprint != p1.exact_digest  # normalised vs exact-byte identity, distinct
    assert p1.mnemonic_sequence == tuple(i.mnemonic for i in p1.insns)
    assert p1.n_insns == len(MIPS_WORDS)
    assert p1.policy == NORMALISATION_POLICY and p1.decoder_version == scoring.DECODER_VERSION
    assert scoring.asm_text(f) == "\n".join(f"{i.offset:5x}:  {i.text}" for i in b_disassemble(f))


# --------------------------------------------------------------------------- S-02

def test_s02_score_only_and_render_paths_agree_on_scores_and_verdicts():
    for arch, golden in GOLDEN:
        for name, t, c in golden:
            d = diff(t, c)
            s = score_only(t, c)
            assert (s.score, s.max_score, s.bytes_match) == (d.score, d.max_score, d.bytes_match), (arch, name)
            assert s.percent == d.percent, (arch, name)
            assert s._rows is None, (arch, name)  # display rows are deferred, not built eagerly
            assert s.render() == d.render(), (arch, name)
            assert s.rows() == d.rows, (arch, name)
            assert s.to_diff_result().rows == d.rows, (arch, name)
            # the compact counts reconcile exactly with the score under the verdict rules
            penalty = sum(s.counts[k] * COUNT_PENALTIES[k] for k in COUNT_PENALTIES)
            expected = 0 if s.bytes_match else (1 if penalty == 0 else penalty)
            assert s.score == expected, (arch, name)
            assert list(s.counts) == ["register", "stack_offset", "operand", "insertion", "deletion", "reorder",
                                      "call_reference"]
            assert s.counts["call_reference"] <= s.counts["operand"]  # informational subset, no own penalty
            assert s.summary["schema_version"] == 1
            assert set(s.summary) == {"schema_version", "counts", "observations", "hypotheses", "coverage", "verification"}
            assert s.summary["verification"] == {"function_link_match": "unverified"}
            assert s.summary["coverage"] == {"status": "complete"}
            assert s.summary["counts"] == s.counts


def test_s02_summary_observations_are_facts_only():
    exact = score_only(obj("x86_64", False, X64_IMM), obj("x86_64", False, X64_IMM))
    assert any("match exactly" in o for o in exact.summary["observations"])
    assert exact.summary["hypotheses"] == []  # hypotheses are never invented by scoring
    # textually identical but byte-different: observed, not claimed identical
    t = obj("x86_64", False, bytes.fromhex("eb 00 90 c3"))
    c = obj("x86_64", False, bytes.fromhex("e9 fd ff ff ff 90 c3"))
    s = score_only(t, c)
    assert not s.bytes_match and s.score == 1
    assert any("read identically" in o for o in s.summary["observations"])
    assert all("control flow" not in o.lower() for o in s.summary["observations"])
    with pytest.raises(ValueError, match="coverage status"):
        scoring.mismatch_summary({"register": 0}, bytes_match=False, score=1, coverage="bogus")
    with pytest.raises(ValueError, match="function_link_match"):
        scoring.mismatch_summary({"register": 0}, bytes_match=False, score=1, function_link_match="maybe")


# --------------------------------------------------------------------------- S-03

def test_s03_same_text_different_encoding_is_not_an_exact_match():
    # short jmp vs near jmp to the same position-relative target: identical rows, different bytes
    t = obj("x86_64", False, bytes.fromhex("eb 00 90 c3"))
    c = obj("x86_64", False, bytes.fromhex("e9 fd ff ff ff 90 c3"))
    d = diff(t, c)
    assert [m for m, _, _ in d.rows] == [" ", " ", " "]
    assert all(tt == cc for _, tt, cc in d.rows)  # same disassembly text on both sides
    assert not d.bytes_match and d.score == 1  # exact verification rejects the encoding difference
    pt, pc = prepare(t), prepare(c)
    assert pt.exact_digest != pc.exact_digest  # exact-byte identity differs
    assert pt.normalised_fingerprint != pc.normalised_fingerprint

    # same text, same bytes, but the relocation type differs: the symbolic fingerprint differs
    code = bytes.fromhex("48 8b 05 00 00 00 00 c3")
    t2 = obj("x86_64", False, code, [Reloc(3, 2, "foo", 0)])  # R_X86_64_PC32
    c2 = obj("x86_64", False, code, [Reloc(3, 4, "foo", 0)])  # R_X86_64_PLT32
    d2 = diff(t2, c2)
    assert [m for m, _, _ in d2.rows] == [" ", " "] and all(tt == cc for _, tt, cc in d2.rows)
    assert not d2.bytes_match and d2.score == 1
    s2 = score_only(t2, c2)
    assert s2.summary["verification"] == {"function_link_match": "unverified"}  # scoring never links
    assert s2.summary["coverage"] == {"status": "complete"}


# --------------------------------------------------------------------------- S-04

def test_s04_literal_difference_prevents_acceptance():
    from decompnet.rom.match import LinkResult, diff as rom_diff

    t = obj("x86_64", False, X64_IMM)   # mov eax, 1; ret
    c = obj("x86_64", False, X64_IMM2)  # mov eax, 2; ret — code aligns, the literal differs
    d = diff(t, c)
    assert not d.bytes_match
    assert d.score == PENALTY_OPERAND
    s = score_only(t, c)
    assert s.counts["operand"] == 1 and sum(s.counts.values()) == 1
    assert s.summary["verification"] == {"function_link_match": "unverified"}
    # the linked judge's literal/data check decides acceptance, not the textual score (P3.3.3):
    assert not rom_diff(t, LinkResult(c, False)).bytes_match  # linker found the data difference
    # even a normalised fingerprint match is not accepted when linking says the image differs:
    same = obj("x86_64", False, X64_IMM)
    overruled = rom_diff(t, LinkResult(same, False))
    assert not overruled.bytes_match and overruled.score >= 1
    verified = rom_diff(t, LinkResult(same, True))
    assert verified.bytes_match and verified.score == 0


# --------------------------------------------------------------------------- S-05

def test_s05_relocation_index_handles_multiple_relocs_and_variable_width():
    # two relocations inside one 10-byte instruction
    code = bytes.fromhex("48 b8" + "00" * 8 + "c3")
    rels = [Reloc(2, 2, "a", 0), Reloc(6, 2, "b", 0)]
    f = obj("x86_64", False, code, rels)
    p = prepare(f)
    mov, ret = p.insns
    assert (mov.size, mov.mnemonic) == (10, "movabs") and ret.size == 1
    assert p.reloc_index[0] == (rels[0], rels[1])
    assert p.relocs_at(2) == (rels[0], rels[1]) and p.relocs_at(6) == (rels[0], rels[1])
    assert p.relocs_at(0) == (rels[0], rels[1]) and p.relocs_at(11) == ()
    check_relocs_indexed(f, p)

    # MIPS %hi/%lo: one relocation per instruction, paired symbolization
    mips = obj("mips", True, be(0x3C1C0000, 0x279C0000, 0x03E00008, 0),
               [Reloc(0, 5, "_gp", 0), Reloc(4, 6, "_gp", 0)])
    pm = prepare(mips)
    assert pm.insns[0].text == "lui $gp, %hi(_gp)" and pm.insns[1].text == "addiu $gp, $gp, %lo(_gp)"
    assert pm.reloc_index == {0: (Reloc(0, 5, "_gp", 0),), 1: (Reloc(4, 6, "_gp", 0),)}
    check_relocs_indexed(mips, pm)

    # Thumb bl: one relocation spanning a whole 4-byte instruction among 2-byte instructions
    thumb = obj("thumb", False, THUMB_BASE, THUMB_RELOC)
    pt = prepare(thumb)
    assert [i.size for i in pt.insns] == [2, 2, 4, 2, 2]
    assert pt.reloc_index == {2: (Reloc(4, 10, "func", 0),)}
    check_relocs_indexed(thumb, pt)
    # the indexed symbolization matches the baseline on this variable-width ISA too
    assert [i.text for i in pt.insns] == [i.text for i in b_disassemble(thumb)]

    # every golden fixture's relocations are fully indexed
    for arch, golden in GOLDEN:
        for name, t, c in golden:
            for f in (t, c):
                if f.relocs:
                    check_relocs_indexed(f, prepare(f))


# --------------------------------------------------------------------------- S-06

def test_s06_prepare_cache_invalidates_on_key_changes(monkeypatch):
    prepare_cache_clear()
    before = prepare_cache_info()
    f = obj("mips", True, MIPS_BASE, MIPS_RELOC)
    p1 = prepare(f)
    info = prepare_cache_info()
    assert info["entries"] == 1 and info["misses"] == before["misses"] + 1
    assert prepare(f) is p1 and prepare_cache_info()["hits"] == info["hits"] + 1

    # decoder version change: same content, new identity (old entry never reused for the new key)
    orig_version = scoring.DECODER_VERSION
    monkeypatch.setattr(scoring, "DECODER_VERSION", "test-decoder-9")
    p2 = prepare(f)
    assert p2 is not p1 and prepare_cache_info()["entries"] == 2
    assert [i.text for i in p2.insns] == [i.text for i in p1.insns]
    monkeypatch.setattr(scoring, "DECODER_VERSION", orig_version)
    assert prepare(f) is p1  # the original identity still hits its own entry

    # normalisation policy change: a registered second policy is a new identity
    monkeypatch.setitem(scoring._NORMALISERS, "normalise-v2",
                        lambda ins, ops, covering, names, arch: ops.upper())
    p3 = prepare(f, "normalise-v2")
    assert p3 is not p1 and p3.policy == "normalise-v2"
    assert p3.insns[0].text != p1.insns[0].text
    assert prepare(f, NORMALISATION_POLICY) is p1  # unchanged key still served
    with pytest.raises(ValueError, match="unknown normalisation policy"):
        prepare(f, "normalise-v9")  # unregistered policies are rejected, never silently reused

    # processor mode change (endianness) with the same words: a different prepared target
    fle = obj("mips", False, MIPS_BASE_LE, MIPS_RELOC)
    p4 = prepare(fle)
    assert p4 is not p1 and p4.insns[0].text == p1.insns[0].text  # same source, different mode identity
    assert prepare_cache_info()["entries"] == 4

    prepare_cache_clear()
    assert prepare_cache_info()["entries"] == 0
    assert prepare(f) is not p1  # rebuilt from scratch, never a stale object


# --------------------------------------------------------------------------- S-07

def test_s07_alternative_alignment_changes_ranking_only_when_versioned():
    t = obj("x86_64", False, X64_BASE)
    reg = obj("x86_64", False, X64_REG)
    ins = obj("x86_64", False, X64_INS)
    baseline_reg, baseline_ins = diff(t, reg).score, diff(t, ins).score
    assert baseline_reg < baseline_ins  # the baseline ranking these fixtures exercise

    # the baseline version can never be overwritten, and unknown versions are rejected
    with pytest.raises(ValueError, match="already registered"):
        scoring.register_aligner(ALIGNMENT_VERSION, lambda a, b: [])
    with pytest.raises(ValueError, match="unknown alignment version"):
        diff(t, reg, alignment_version="never-registered-v1")
    assert ALIGNMENT_VERSION == "sequence-v1"

    def replace_all(t_ins, c_ins):
        return [("replace", 0, len(t_ins), 0, len(c_ins))] if t_ins or c_ins else []

    scoring.register_aligner("alt-replace-v1", replace_all)
    try:
        # registering an alternative does not change the default ranking...
        assert diff(t, reg).score == baseline_reg and diff(t, ins).score == baseline_ins
        assert score_only(t, reg).score == baseline_reg
        # ...and the alternative only takes effect under its own version, where it may re-rank
        alt_reg, alt_ins = (diff(t, reg, alignment_version="alt-replace-v1").score,
                            diff(t, ins, alignment_version="alt-replace-v1").score)
        assert alt_reg != baseline_reg and alt_ins != baseline_ins
        assert alt_reg > alt_ins  # the alternative genuinely re-ranks this pair
        assert score_only(t, reg, alignment_version="alt-replace-v1").score == alt_reg
        with pytest.raises(ValueError, match="already registered"):
            scoring.register_aligner("alt-replace-v1", replace_all)
        # the normalisation-policy gate mirrors the aligner gate
        with pytest.raises(ValueError, match="already registered"):
            scoring.register_normalisation_policy(NORMALISATION_POLICY, lambda *a: "x")
    finally:
        del scoring._ALIGNERS["alt-replace-v1"]


# ------------------------------------------------------- P3.3 fast path, P3.1 sequence reuse, P5.4 counts

def test_p33_authoritative_verdict_skips_alignment_but_a_fingerprint_alone_never_does(monkeypatch):
    prepare_cache_clear()
    t = obj("x86_64", False, X64_BASE)
    same = obj("x86_64", False, X64_BASE)
    other = obj("x86_64", False, X64_REG)
    expected = score_only(t, other)  # scored before the guards below are installed
    full = score_only(t, same)
    calls = []
    real_align, real_compare = scoring._align, scoring._compare
    monkeypatch.setattr(scoring, "_align", lambda *a, **k: calls.append("align") or real_align(*a, **k))
    monkeypatch.setattr(scoring, "_compare", lambda *a, **k: calls.append("compare") or real_compare(*a, **k))

    fast = score_only(t, same, verdict=True)
    assert calls == []  # no alignment and no comparison ran
    assert (fast.score, fast.bytes_match, fast.max_score) == (0, True, full.max_score)
    assert all(v == 0 for v in fast.counts.values()) and list(fast.counts) == list(full.counts)
    assert fast.summary["counts"] == fast.counts
    assert fast.summary["verification"] == {"function_link_match": "exact"}
    assert fast.percent == 100.0
    assert fast.render() == full.render()  # rows are still available on demand

    # a fingerprint match alone (no verdict, or a negative one) is still aligned and scored
    for v in (None, False):
        calls.clear()
        res = score_only(t, same, verdict=v)
        assert "align" in calls and "compare" in calls
        assert (res.score, res.bytes_match) == (0, True)
        assert res.summary["verification"] == {"function_link_match": "unverified"}

    # a verdict never overrides a normalised-fingerprint difference: the candidate is scored normally
    calls.clear()
    res = score_only(t, other, verdict=True)
    assert "align" in calls
    assert (res.score, res.bytes_match, res.counts) == (expected.score, False, expected.counts)
    assert res.summary["verification"] == {"function_link_match": "unverified"}
    with pytest.raises(ValueError, match="unknown alignment version"):
        score_only(t, same, verdict=True, alignment_version="never-registered-v1")


def test_p31_baseline_aligner_reuses_the_prepared_mnemonic_sequences(monkeypatch):
    prepare_cache_clear()
    t = obj("x86_64", False, X64_BASE)
    c = obj("x86_64", False, X64_INS)
    pt, pc = prepare(t), prepare(c)
    seen = []
    real = difflib.SequenceMatcher

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(scoring.difflib, "SequenceMatcher", spy)
    s = score_only(t, c)
    d = diff(t, c)
    rendered = s.rows()
    assert len(seen) == 3  # score_only, diff and the deferred rows: one alignment each
    assert all(k["a"] is pt.mnemonic_sequence and k["b"] is pc.mnemonic_sequence for k in seen)
    monkeypatch.undo()
    assert rendered == d.rows == b_diff(t, c)[2]  # unchanged result: golden parity holds


def test_p54_call_and_global_reference_differences_are_counted_conservatively():
    def counts(t, c):
        s = score_only(t, c)
        assert sum(s.counts[k] * COUNT_PENALTIES[k] for k in COUNT_PENALTIES) == s.score  # no extra penalty
        assert s.summary["counts"] == s.counts
        return s.counts

    call = bytes.fromhex("e8 00 00 00 00 c3")
    # relocated call to a different callee: a call-target reference difference, still an operand mismatch
    c = counts(obj("x86_64", False, call, [Reloc(1, 4, "foo", -4)]),
               obj("x86_64", False, call, [Reloc(1, 4, "bar", -4)]))
    assert (c["operand"], c["call_reference"]) == (1, 1)
    # relocated global data reference to a different symbol
    load = bytes.fromhex("48 8b 05 00 00 00 00 c3")
    c = counts(obj("x86_64", False, load, [Reloc(3, 2, "g1", -4)]),
               obj("x86_64", False, load, [Reloc(3, 2, "g2", -4)]))
    assert (c["operand"], c["call_reference"]) == (1, 1)
    # the same symbol with a different addend is not a reference difference: unknown stays operand
    c = counts(obj("x86_64", False, load, [Reloc(3, 2, "g1", -4)]),
               obj("x86_64", False, load, [Reloc(3, 2, "g1", 0)]))
    assert (c["operand"], c["call_reference"]) == (1, 0)
    # an unrelocated direct call to a different address is a call-target difference
    c = counts(obj("x86_64", False, bytes.fromhex("e8 05 00 00 00 c3")),
               obj("x86_64", False, bytes.fromhex("e8 10 00 00 00 c3")))
    assert (c["operand"], c["call_reference"]) == (1, 1)
    # a plain immediate difference and register-only differences are never call references
    c = counts(obj("x86_64", False, X64_IMM), obj("x86_64", False, X64_IMM2))
    assert (c["operand"], c["call_reference"]) == (1, 0)
    c = counts(obj("x86_64", False, X64_BASE), obj("x86_64", False, X64_REG))
    assert c["call_reference"] == 0 and c["register"] > 0
