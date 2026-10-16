"""maspsx load-delay-nop drop spike (plan C.2): transformation rules on synthetic asm, plus an
opt-in byte-level check against the real GNU as 2.6 when the PSX toolchain is installed.

The pass drops the ``nop`` between a load and a following j/jr/jal only when the jump does not read
the loaded register (maspsx semantics: maspsx itself materialises that nop precisely when the next
instruction reads the loaded register — the case we must keep). Conditional branches, intervening
labels, extra nops and non-``.set`` directives block the pattern.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest
from elftools.elf.elffile import ELFFile

from decompnet import toolchains
from decompnet.rom.maspsx import NopDropDecision, NopDropReport, drop_delay_nops, norm_reg

SAFE = """\t.text
\t.ent\tsafe_jr
safe_jr:
\tlw\t$v0,0($sp)
\tnop
\tjr\t$ra
\tnop
\t.end\tsafe_jr
"""


def decisions(report: NopDropReport):
    return [(d.function, d.line, d.action) for d in report.decisions]


# ------------------------------------------------------------- round trip

def test_round_trip_identity():
    """Asm without the pattern is byte-identical, with no decisions (the pass never reformats)."""
    for src in (SAFE.replace("\tnop\n", ""), "\t.text\n\tjr\t$ra\n\tnop\n", ""):
        out, rep = drop_delay_nops(src)
        assert out == src
        assert rep.decisions == () and rep.changed is False and rep.dropped == 0 and rep.kept == 0


# ---------------------------------------------------------- safe drops

def test_safe_jr_nop_dropped():
    out, rep = drop_delay_nops(SAFE)
    lines = out.split("\n")
    assert "\tjr\t$ra" in out and "\tnop" in out  # the jump's own delay-slot nop remains
    assert lines[lines.index("safe_jr:") + 1] == "\tlw\t$v0,0($sp)"
    assert lines[lines.index("safe_jr:") + 2] == "\tjr\t$ra"  # jump moved into the load's delay slot
    assert rep.changed and rep.dropped == 1 and rep.kept == 0
    d = rep.decisions[0]
    assert d.action == "dropped" and d.function == "safe_jr" and d.line == 5
    assert "does not read" in d.reason


def test_safe_j_and_jal_dropped():
    for op in ("j\t.L5", "jal\tsomewhere"):
        src = f"\t.ent\tf\nf:\n\tlw\t$2,8($4)\n\tnop\n\t{op}\n\tnop\n\t.end\tf\n"
        out, rep = drop_delay_nops(src)
        assert rep.dropped == 1 and f"\t{op}" in out
        assert "\tnop\n\t" + op not in out  # the delay nop is gone


def test_safe_drop_keeps_every_other_line_byte_identical():
    src = """\t.text
\t.ent\tmix
mix:
\tlw\t$v0,0($sp)   # first load
\tnop
\tjr\t$ra
\tnop
\tlw\t$t9,4($sp)   # second load (unsafe)
\tnop
\tjr\t$t9
\tnop
\t.end\tmix
"""
    out, rep = drop_delay_nops(src)
    original = src.split("\n")
    assert rep.dropped == 1 and rep.kept == 1
    remaining = [l for l in original if l.strip() != "nop"]
    assert remaining == [l for l in out.split("\n") if l.strip() != "nop"]


# ---------------------------------------------------------- unsafe keeps

def test_unsafe_jr_through_loaded_register_kept():
    src = "\t.ent\tbad\nbad:\n\tlw\t$t9,0($sp)\n\tnop\n\tjr\t$t9\n\tnop\n\t.end\tbad\n"
    out, rep = drop_delay_nops(src)
    assert out == src and rep.changed is False
    d = rep.decisions[0]
    assert d.action == "kept" and "reads the loaded register" in d.reason
    assert d.load.startswith("lw") and d.jump.startswith("jr")


def test_register_alias_spellings_compare_canonically():
    # $2 and $v0 are the same register: dropping would jump through the stale value
    alias = "\t.ent\tal\nal:\n\tlw\t$2,0($sp)\n\tnop\n\tjr\t$v0\n\tnop\n\t.end\tal\n"
    out, rep = drop_delay_nops(alias)
    assert out == alias and rep.decisions[0].action == "kept"
    # $2 vs $31 ($ra) are different registers: safe to drop
    other = "\t.ent\tok\nok:\n\tlw\t$2,0($sp)\n\tnop\n\tjr\t$31\n\tnop\n\t.end\tok\n"
    out2, rep2 = drop_delay_nops(other)
    assert rep2.decisions[0].action == "dropped"
    assert norm_reg("$v0") == norm_reg("$2") and norm_reg("$ra") == norm_reg("$31")
    assert norm_reg("$v0") != norm_reg("$31")


def test_maspsx_register_jump_spelling():
    """maspsx emits ``j $2`` for a register jump: it reads $2, so the nop must stay."""
    src = ("\t.ent\tjmp\njmp:\n\tlw\t$2,%lo($L30)($at)\n\tnop # DEBUG: Reuse of '$2'. 'j\t$2'\n"
           "\tj\t$2\n\tnop\n\t.end\tjmp\n")
    out, rep = drop_delay_nops(src)
    assert out == src and rep.decisions[0].action == "kept"
    # but `j $31` after a load of $2 is unrelated
    safe = src.replace("\tj\t$2", "\tj\t$31").replace("'j\t$2'", "'j\t$31'")
    out2, rep2 = drop_delay_nops(safe)
    assert rep2.decisions[0].action == "dropped"


# ------------------------------------------------- pattern blockers

def test_branches_are_never_touched():
    src = ("\t.ent\tb\nb:\n\tlw\t$2,0($4)\n\tnop\n\tbeq\t$2,$0,.Lend\n\tnop\n.Lend:\n"
           "\tjr\t$ra\n\tnop\n\t.end\tb\n")
    out, rep = drop_delay_nops(src)
    assert out == src and rep.decisions == ()


def test_labels_between_load_and_jump_block_the_pattern():
    # a label between the load and its nop: the nop may be a branch target
    between = "\t.ent\tl\nl:\n\tlw\t$v0,0($sp)\n.Lx:\n\tnop\n\tjr\t$ra\n\tnop\n\t.end\tl\n"
    out, rep = drop_delay_nops(between)
    assert out == between and rep.decisions == ()
    # a label between the nop and the jump: same refusal
    after = "\t.ent\tl2\nl2:\n\tlw\t$v0,0($sp)\n\tnop\n.Ly:\n\tjr\t$ra\n\tnop\n\t.end\tl2\n"
    out2, rep2 = drop_delay_nops(after)
    assert out2 == after and rep2.decisions == ()


def test_two_nops_are_not_touched():
    src = "\t.ent\tt\nt:\n\tlw\t$v0,0($sp)\n\tnop\n\tnop\n\tjr\t$ra\n\tnop\n\t.end\tt\n"
    out, rep = drop_delay_nops(src)
    assert out == src and rep.decisions == ()


def test_non_set_directives_block_the_pattern():
    src = "\t.ent\td\nd:\n\tlw\t$v0,0($sp)\n\tnop\n\t.align\t2\n\tjr\t$ra\n\tnop\n\t.end\td\n"
    out, rep = drop_delay_nops(src)
    assert out == src and rep.decisions == ()


# ------------------------------------------------- transparency rules

def test_set_directives_comments_and_blanks_are_transparent():
    # maspsx's EXPAND_AT shape: load; .set at; comment; nop; jump - recognised, and the safety rule
    # still applies (the jump reads the loaded register here, so it is kept)
    kept = ("\t.ent\tk\nk:\n\tlw\t$2,%lo($L30)($at)\n\t.set\tat\n# EXPAND_AT END\n"
            "\tnop # DEBUG: Reuse of '$2'. 'j\t$2'\n\tj\t$2\n\tnop\n\t.end\tk\n")
    out, rep = drop_delay_nops(kept)
    assert out == kept and rep.decisions[0].action == "kept"
    # the safe variant of the same shape is dropped
    dropped = kept.replace("\tj\t$2\n\tnop", "\tj\t$31\n\tnop").replace("'j\t$2'", "'j\t$31'")
    out2, rep2 = drop_delay_nops(dropped)
    assert rep2.decisions[0].action == "dropped" and "\tnop # DEBUG" not in out2


# ------------------------------------------------- reporting

def test_decisions_carry_function_line_and_action():
    src = ("\t.ent\tone\none:\n\tlw\t$v0,0($sp)\n\tnop\n\tjr\t$ra\n\tnop\n\t.end\tone\n"
           "\t.ent\ttwo\ntwo:\n\tlw\t$t9,0($sp)\n\tnop\n\tjr\t$t9\n\tnop\n\t.end\ttwo\n")
    out, rep = drop_delay_nops(src)
    assert decisions(rep) == [("one", 4, "dropped"), ("two", 11, "kept")]
    assert rep.affected_functions() == {"one": {"dropped": 1, "kept": 0},
                                        "two": {"dropped": 0, "kept": 1}}
    assert rep.dropped == 1 and rep.kept == 1 and rep.changed


def test_decision_is_a_stable_record():
    d = NopDropDecision("f", 3, "lw\t$2,0($4)", "jr\t$ra", "dropped", "reason")
    assert d.action == "dropped" and d.line == 3 and d.function == "f" and d.reason == "reason"


# ----------------------------------------------------- toolchains registry

def test_registry_is_opt_in_and_version_gated():
    assert toolchains.asm_pass("maspsx-drop-delay-nops") is not None
    assert toolchains.asm_pass("no-such-pass") is None
    assert toolchains.apply_asm_pass("maspsx-drop-delay-nops", SAFE) != SAFE  # applied when asked
    with pytest.raises(ValueError):
        toolchains.apply_asm_pass("no-such-pass", SAFE)  # unknown names are rejected explicitly
    with pytest.raises(ValueError):
        toolchains.register_asm_pass("maspsx-drop-delay-nops", drop_delay_nops)  # baseline name
    with pytest.raises(ValueError):
        toolchains.register_asm_pass("Not_A_Name", drop_delay_nops)


# -------------------------------------- byte-level check with the real as

def _as_text(lines: str) -> bytes:
    d = Path(tempfile.mkdtemp())
    (d / "in.s").write_text(lines)
    as_path = str(toolchains.require("gcc-2.7.2-psx") / "as")
    r = subprocess.run([as_path, "-EL", str(d / "in.s"), "-o", str(d / "in.o")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    with open(d / "in.o", "rb") as f:
        return ELFFile(f).get_section_by_name(".text").data()


needs_psx_as = pytest.mark.skipif(toolchains.installed("gcc-2.7.2-psx") is None,
                                  reason="needs the gcc-2.7.2-psx toolchain (bundled GNU as 2.6)")


@needs_psx_as
def test_byte_level_drop_matches_the_assembled_transform():
    """The pass's output assembles to exactly the original minus the dropped nop word. (binutils 2.6
    pads .text to a 16-byte multiple, so compare the instruction streams, not the padded tail.)"""
    src = ".text\n\t.set noreorder\nsafe_fn:\n\tlw\t$v0,0($sp)\n\tnop\n\tjr\t$ra\n\tnop\n"
    out, rep = drop_delay_nops(src)
    assert rep.dropped == 1
    orig_bytes = _as_text(src)
    pass_bytes = _as_text(out)
    # lw | jr | nop  ==  lw | (dropped nop) | jr | nop
    assert pass_bytes[:12] == orig_bytes[:4] + orig_bytes[8:16]
    # the unsafe case assembles identically (the pass must be a no-op)
    unsafe = ".text\n\t.set noreorder\nbad_fn:\n\tlw\t$t9,0($sp)\n\tnop\n\tjr\t$t9\n\tnop\n"
    out2, rep2 = drop_delay_nops(unsafe)
    assert rep2.decisions[0].action == "kept" and _as_text(out2) == _as_text(unsafe)


@needs_psx_as
def test_pass_does_not_break_real_emitted_asm():
    """The pass runs over a real gcc-2.7.2-psx unit and leaves it assemblable and unchanged
    (the demo corpus carries no safe drop pattern: its only load->nop->jump is the kept unsafe one)."""
    root = Path(__file__).resolve().parent.parent
    game = root / "projects" / "demo-n64" / "original" / "game.c"
    if not game.exists():
        pytest.skip("demo game source missing")
    tc = str(toolchains.require("gcc-2.7.2-psx"))
    out_s = Path(tempfile.mkdtemp()) / "inc.s"
    r = subprocess.run([f"{tc}/gcc", f"-B{tc}/", "-O2", "-G8", "-c", "-S", "-Wa,-EL",
                        f"-I{game.parent}", str(game), "-o", str(out_s)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("gcc-2.7.2-psx could not compile the demo game on this host")
    asm = out_s.read_text()
    out, rep = drop_delay_nops(asm)
    assert out == asm  # no safe pattern in this corpus: nothing to drop, nothing reformatted
    assert _as_text(out) == _as_text(asm)  # still assembles to the same bytes
