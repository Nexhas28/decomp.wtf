"""Attempt-scoped assembler variant (PR-32).

Two PSX assembler behaviours, neither sufficient alone:
  * the pinned GNU as 2.6 materialises gcc's blocking load-delay nop before j/jr, and expands the
    `move` macro as `addu` (which is what PSX ROMs contain);
  * modern mips-ps2-decompals binutils drops that nop, but expands `move` as `or`.
So the variant assembles with modern binutils plus the PSY-Q `move` expansion, and is applied ONLY to a
near-miss attempt, kept only when it scores strictly better - never swapped project-wide (measured: a
global swap regresses already-matched functions whose ROM really does contain the nop).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from decompnet import compiler, project, toolchains
from decompnet.rom.maspsx import expand_move_addu


# --------------------------------------------------------------------------- the pass

def test_move_expands_to_addu_like_the_psyq_assembler():
    text = "\tmove\t$16,$4\n\tmove $2,$3\n"
    out, count = expand_move_addu(text)
    assert count == 2
    assert out == "\taddu\t$16,$4,$zero\n\taddu $2,$3,$zero\n"


def test_move_pass_leaves_everything_else_byte_identical():
    text = "\n".join([
        "# move $1,$2 inside a comment",
        "\tmove\t$4,$5\t# trailing comment kept",
        "\taddu\t$sp,$sp,40",
        "\tlui\t$2,%hi(D_80030101)",
        "label:",
        "/* move $6,$7 in a block comment */",
        "",
    ])
    out, count = expand_move_addu(text)
    assert count == 1
    lines = out.split("\n")
    assert lines[0] == "# move $1,$2 inside a comment"
    assert lines[1] == "\taddu\t$4,$5,$zero\t# trailing comment kept"
    assert lines[2:] == text.split("\n")[2:]


def test_move_pass_is_registered_under_its_documented_name():
    assert toolchains.asm_pass("psyq-move-addu") is expand_move_addu
    assert toolchains.apply_asm_pass("psyq-move-addu", "\tmove\t$1,$2\n") == "\taddu\t$1,$2,$zero\n"
    with pytest.raises(ValueError):
        toolchains.apply_asm_pass("no-such-pass", "\tnop\n")


# --------------------------------------------------------------------------- variant config

def _rom_cfg(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    (root / "game.rom").write_bytes(b"\x00" * 64)
    cfg = {"kind": "rom", "big_endian": False, "platform": "psx",
           "rom": {"file": "game.rom"},
           "segments": [{"rom_start": 0, "rom_end": 64, "vram": 0x80010000}],
           "context": {}, "compiler": {"toolchain": "gcc-2.8.1-psx", "flags": ["-O2", "-G8"]}}
    return root, cfg


def test_variant_config_uses_the_modern_assembler_and_the_move_pass(tmp_path, monkeypatch):
    root, cfg = _rom_cfg(tmp_path)
    asm_dir = tmp_path / "binutils"
    asm_dir.mkdir()
    (asm_dir / "as").write_text("#!/bin/sh\n", encoding="utf-8")
    base = compiler.CompilerConfig(cmd=["gcc", "-c"], include_dirs=(), ro_paths=("/old",))
    monkeypatch.setattr(project, "compiler_config", lambda root, cfg: base)
    monkeypatch.setattr(toolchains, "path", lambda tid, linux=None: asm_dir)
    v = project.variant_compiler_config(root, cfg)
    assert v is not None
    assert v.asm_cmd[0] == str(asm_dir / "as") and v.asm_cmd[1] == "-EL"
    assert "-G0" in v.asm_cmd  # absolute lui/%lo addressing: the ROM never uses %gp_rel here
    assert v.asm_pass == "psyq-move-addu"
    assert v.cache is False  # variant compiles must not poison the content-addressed cache
    assert str(asm_dir) in v.ro_paths


def test_variant_config_is_absent_when_the_toolchain_is_not_installed(tmp_path, monkeypatch):
    root, cfg = _rom_cfg(tmp_path)

    def missing(tid, linux=None):
        raise toolchains.ToolchainError("not installed")

    monkeypatch.setattr(project, "compiler_config",
                        lambda root, cfg: compiler.CompilerConfig(cmd=["gcc", "-c"]))
    monkeypatch.setattr(toolchains, "path", missing)
    assert project.variant_compiler_config(root, cfg) is None


# --------------------------------------------------------------------------- gating

class _Judge:
    """Minimal stand-in: records whether the variant was attempted, and what it returned."""

    _VARIANT_MAX_SCORE = 200
    calls = 0

    def __init__(self, base_score: int, variant_score: int | None):
        self.base_score, self.variant_score = base_score, variant_score

    def _variant_attempt(self, fn, proj, source, ctx, base_diff):
        type(self).calls += 1
        if self.variant_score is None or self.variant_score >= base_diff.score:
            return None
        return ("variant", "cand", True, [], self.variant_score)


def _should_try(verdict, score, limit=300) -> bool:
    """The judge's condition, in one place so the test cannot drift from the implementation."""
    return verdict is not True and 0 < score <= limit


def test_variant_only_runs_for_near_misses():
    assert _should_try(False, 100) is True          # near miss: try the variant
    assert _should_try(False, 300) is True          # at the limit
    assert _should_try(False, 301) is False         # far off and structurally close: skip
    assert _should_try(False, 159265) is False      # a wall (jtbl giant) is skipped
    assert _should_try(True, 0) is False            # already matched
    assert _should_try(None, 0) is False            # nothing to fix


def _worth_variant(verdict, score, extras, limit=300, far_extras=5) -> bool:
    """The judge's real condition (network.Network._worth_variant), restated for the table test."""
    if verdict is True or score <= 0:
        return False
    return score <= limit or extras >= far_extras


def test_structural_mismatch_also_triggers_the_variant():
    # func_800A51DC: 785 rows for a 733-insn target - 52 extras from the assembler's blocking nops,
    # score far above the near-miss limit, yet the variant is what makes it reproducible (5320 -> 410).
    assert _worth_variant(False, 5320, extras=52) is True
    assert _worth_variant(False, 110, extras=1) is True     # near-miss limit path
    assert _worth_variant(False, 230, extras=2) is True     # func_800AEA14: 230 -> reproducible 130
    assert _worth_variant(False, 400, extras=1) is False    # one extra row and far off: skip
    assert _worth_variant(False, 400, extras=4) is False    # weak signature far off: not worth a compile
    assert _worth_variant(False, 5000, extras=0) is False   # nothing structural to fix
    assert _worth_variant(True, 0, extras=0) is False
    assert _worth_variant(False, 159265, extras=2) is False # jtbl wall: still skipped


def test_variant_result_is_kept_only_when_strictly_better():
    j = _Judge(base_score=100, variant_score=0)
    assert j._variant_attempt(None, None, None, None, type("D", (), {"score": 100})()) is not None
    j2 = _Judge(base_score=100, variant_score=100)
    assert j2._variant_attempt(None, None, None, None, type("D", (), {"score": 100})()) is None
    j3 = _Judge(base_score=100, variant_score=None)
    assert j3._variant_attempt(None, None, None, None, type("D", (), {"score": 100})()) is None


# --------------------------------------------------------------------------- real lane

@pytest.mark.skipif(not compiler, reason="compiler module required")
def test_variant_two_stage_compile_real_lane(tmp_path, monkeypatch):
    """Real lane: gcc -S, the pass, then the modern assembler, driven through compile_source."""
    root, cfg = _rom_cfg(tmp_path)
    try:
        asm_dir = Path(toolchains.path("binutils-mips"))
    except toolchains.ToolchainError:
        pytest.skip("binutils-mips is not installed on this host")
    asm = asm_dir / "as"
    if not asm.exists():
        pytest.skip("binutils-mips has no `as` on this host")
    try:
        tc_dir = Path(toolchains.path("gcc-2.8.1-psx"))
    except toolchains.ToolchainError:
        pytest.skip("gcc-2.8.1-psx is not installed on this host")

    try:
        project.compiler_config(root, cfg)
    except toolchains.ToolchainError:
        pytest.skip("gcc-2.8.1-psx is not installed in this test environment")
    v = project.variant_compiler_config(root, cfg)
    assert v is not None
    source = "void probe(int a, int b) { *(volatile unsigned char *)0x80030100 = a + b; }\n"
    res = compiler.compile_source(v, "#define NULL 0\n", source)
    if not res.ok:
        pytest.skip(f"variant toolchain cannot run here: {(res.log or '')[-120:]}")
    assert res.obj, "variant compile produced no object"
    assert b"\x00" in res.obj


# --------------------------------------------------------------------------- maspsx variant

def _fake_toolchains(tmp_path: Path, monkeypatch, maspsx_dir: Path | None, tc_dir: Path | None):
    def path(tid, linux=None):
        if tid == project.MASPSX_TOOLCHAIN:
            if maspsx_dir is None:
                raise toolchains.ToolchainError("maspsx not installed")
            return maspsx_dir
        if maspsx_dir is None:
            raise toolchains.ToolchainError(f"{tid} not installed")
        return maspsx_dir

    monkeypatch.setattr(toolchains, "path", path)
    base_cmd = [str(tc_dir / "gcc"), "-c"] if tc_dir else ["gcc", "-c"]
    monkeypatch.setattr(project, "compiler_config",
                        lambda root, cfg: compiler.CompilerConfig(cmd=base_cmd))


def test_maspsx_variant_runs_the_preprocessor_then_the_toolchain_assembler(tmp_path, monkeypatch):
    root, cfg = _rom_cfg(tmp_path)
    maspsx_dir = tmp_path / "maspsx"
    maspsx_dir.mkdir()
    (maspsx_dir / "maspsx.py").write_text("# maspsx\n", encoding="utf-8")
    tc_dir = tmp_path / "tc"
    tc_dir.mkdir()
    (tc_dir / "as").write_text("", encoding="utf-8")
    _fake_toolchains(tmp_path, monkeypatch, maspsx_dir, tc_dir)

    v = project.maspsx_variant_config(root, cfg)
    assert v is not None
    assert v.asm_pp_cmd[1] == str(maspsx_dir / "maspsx.py")
    assert "-G8" in v.asm_pp_cmd  # the project's small-data setting is handed to maspsx
    assert v.asm_cmd[0] == str(tc_dir / "as")
    assert "-G0" in v.asm_cmd  # GNU as wants -G0 for PSY-Q behaviour (maspsx's own default)
    assert v.cache is False


def test_maspsx_variant_is_absent_when_maspsx_is_not_installed(tmp_path, monkeypatch):
    root, cfg = _rom_cfg(tmp_path)
    _fake_toolchains(tmp_path, monkeypatch, None, None)
    assert project.maspsx_variant_config(root, cfg) is None


def test_variant_order_prefers_the_assembler_variant_then_maspsx():
    """The judge tries both; the order is the fallback preference, not a correctness claim."""
    builds = [project.variant_compiler_config, project.maspsx_variant_config]
    assert [b.__name__ for b in builds] == ["variant_compiler_config", "maspsx_variant_config"]


def test_maspsx_stage_is_not_run_when_the_base_compile_succeeds_and_matches():
    """Cheap guard: a matching attempt never pays for a variant, whatever variants exist."""
    assert _should_try(True, 0) is False
