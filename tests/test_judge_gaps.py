"""The judge on PlayStation code: per-function compiler comments, whole-function compares and jump tables.

Synthetic code only: a candidate is assembled from hand-written MIPS, the "ROM" is bytes and relocations given
here. Needs the modern MIPS binutils (and, for the compile lane, gcc-2.8.1-psx and maspsx).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from decompnet import directives, network, project, toolchains
from decompnet.compiler import CompilerConfig
from decompnet.network import Network
from decompnet.rom import match
from decompnet.rom.symbols import Symbol, SymbolTable

VRAM = 0x80010000


def _binutils_as() -> Path | None:
    if toolchains.installed("binutils-mips") is None:
        return None
    return project._binutils_as(Path(toolchains.path("binutils-mips")))


needs_as = pytest.mark.skipif(_binutils_as() is None, reason="needs the MIPS binutils (binutils-mips)")


def _assemble(tmp_path: Path, text: str) -> bytes:
    src, obj = tmp_path / "f.s", tmp_path / "f.o"
    src.write_text(".set noreorder\n.set noat\n" + text)
    subprocess.run([str(_binutils_as()), "-EL", "-mips1", "-G0", "-o", str(obj), str(src)], check=True)
    return obj.read_bytes()


def _words(*ws: int) -> bytes:
    return b"".join(w.to_bytes(4, "little") for w in ws)


# ---------------------------------------------------------------- the whole function is compared, not a prefix

@needs_as
def test_a_candidate_longer_than_the_target_never_matches(tmp_path):
    """A 4-byte target (a false split: the frame setup of the next function) and a candidate that starts with
    the same instruction: equal over the target's length, still not a match."""
    obj = _assemble(tmp_path, ".text\n.globl f\nf:\n addiu $sp,$sp,-32\n sw $s0,16($sp)\n jr $ra\n addiu $sp,$sp,32\n")
    target = match.RomTarget("f", VRAM, _words(0x27BDFFE0), [], big_endian=False)
    syms = SymbolTable([Symbol("f", VRAM, 4, "func")])
    linked = match.link(obj, "f", target, syms, lambda a, n: None)
    assert not linked.exact
    assert linked.candidate.code[:4] == target.code  # the prefix alone would have passed


@needs_as
def test_a_candidate_shorter_than_the_target_never_matches(tmp_path):
    obj = _assemble(tmp_path, ".text\n.globl f\nf:\n jr $ra\n nop\n")
    target = match.RomTarget("f", VRAM, _words(0x03E00008, 0, 0x03E00008, 0), [], big_endian=False)
    linked = match.link(obj, "f", target, SymbolTable([Symbol("f", VRAM, 16, "func")]), lambda a, n: None)
    assert not linked.exact


# ---------------------------------------------------------------- jump tables

JTBL = 0x80001000

# gcc's own jump-table address build (lui/addiu of a local .rodata label), one instruction earlier than the ROM's
CANDIDATE_SWITCH = """
.text
.globl f
f:
 lui $at,%hi($L4)
 sll $v0,$a0,2
 addiu $at,$at,%lo($L4)
 addu $at,$at,$v0
 lw $v0,0($at)
 nop
 jr $v0
 nop
$L1:
 jr $ra
 addiu $v0,$zero,1
$L2:
 jr $ra
 addiu $v0,$zero,2
.rdata
$L4:
 .word $L1
 .word $L2
"""


def _switch_target(table: tuple[int, int]) -> tuple[match.RomTarget, dict]:
    """The ROM's form: sll; lui $at,%hi(jtbl); addu; lw %lo(jtbl)($at); its table at JTBL."""
    hi, lo = match.R_MIPS_HI16, match.R_MIPS_LO16
    code = _words(0x00041080, 0x3C018000, 0x00220821, 0x8C221000, 0, 0x00400008, 0,
                  0x03E00008, 0x24020001, 0x03E00008, 0x24020002)
    rom = {JTBL: b"".join(a.to_bytes(4, "little") for a in table)}
    return match.RomTarget("f", VRAM, code, [(0x4, hi, JTBL), (0xC, lo, JTBL)], big_endian=False), rom


def _read(rom: dict):
    def read(addr, n):
        for base, data in rom.items():
            if base <= addr and addr + n <= base + len(data):
                return data[addr - base:addr - base + n]
        return None
    return read


@needs_as
def test_a_jump_table_built_a_few_instructions_away_is_still_the_roms_table(tmp_path):
    obj = _assemble(tmp_path, CANDIDATE_SWITCH)
    syms = SymbolTable([Symbol("f", VRAM, 44, "func"), Symbol("jtbl_80001000", JTBL, 8, "data")])
    target, rom = _switch_target((VRAM + 0x20, VRAM + 0x28))
    linked = match.link(obj, "f", target, syms, _read(rom))
    assert linked.problems == []  # placed at the ROM's jtbl, its entries checked and equal
    assert [set(v.values()) for v in linked.placements.values()] == [{JTBL}]
    assert not linked.exact  # the address build really is different code: the diff shows it


@needs_as
def test_a_jump_table_with_other_entries_is_caught(tmp_path):
    obj = _assemble(tmp_path, CANDIDATE_SWITCH)
    syms = SymbolTable([Symbol("f", VRAM, 44, "func"), Symbol("jtbl_80001000", JTBL, 8, "data")])
    target, rom = _switch_target((VRAM + 0x28, VRAM + 0x20))
    linked = match.link(obj, "f", target, syms, _read(rom))
    assert any("jtbl_80001000 differs from the ROM" in p for p in linked.problems)


# ---------------------------------------------------------------- per-function compiler comments

def test_comments_are_parsed_and_unknown_ones_kept_apart():
    src = ("/* decomp-compiler: gcc-2.7.2-psx */\n/* decomp-assembler: maspsx-2.79-div */\n"
           "/* decomp-cflags: -mno-split-addresses -fno-schedule-insns */\nint f(void) { return 0; }")
    d = directives.parse(src, {"gcc-2.7.2-psx", "gcc-2.8.1-psx"})
    assert (d.compiler, d.assembler, d.cflags, d.unknown) == (
        "gcc-2.7.2-psx", "maspsx-2.79-div", ("-mno-split-addresses", "-fno-schedule-insns"), ())
    d = directives.parse("/* decomp-assembler: maspsx-2.79-ladefer */ /* decomp-compiler: ido-7.1 */",
                         {"gcc-2.8.1-psx"})
    assert d.assembler is None and d.compiler is None
    assert d.unknown == ("decomp-assembler: maspsx-2.79-ladefer", "decomp-compiler: ido-7.1")
    assert not directives.parse("int f(void);").any


@pytest.mark.parametrize("flag", ["-O2", "-o/tmp/x", "-fplugin=evil.so", "-B/tmp", "-fdump-rtl-all", "-include",
                                  "-Wa,-o,x", "-specs=x"])
def test_only_allow_listed_flags_reach_the_compiler(flag):
    assert directives.parse(f"/* decomp-cflags: {flag} */").cflags == ()
    assert directives.parse(f"/* decomp-cflags: {flag} */").unknown == (f"decomp-cflags: {flag}",)


def _proj(tmp_path) -> dict:
    cfg = {"kind": "rom", "big_endian": False, "platform": "psx",
           "compiler": {"toolchain": "gcc-2.8.1-psx", "flags": ["-O2", "-G8"]}}
    return {"root": str(tmp_path), "config": json.dumps(cfg)}


@pytest.fixture
def built(monkeypatch):
    """Network._compiler_for with the config builders recorded instead of run: name -> the compiler block."""
    seen = {}
    for name in ("compiler_config", "maspsx_config", "variant_compiler_config"):
        def builder(root, cfg, *args, name=name):
            seen[name] = (cfg["compiler"], args)
            return CompilerConfig(cmd=["cc"])
        monkeypatch.setattr(network, name, builder)
    return object.__new__(Network), seen


def test_comments_choose_the_compiler_flags_and_assembler(tmp_path, built):
    net, seen = built
    src = ("/* decomp-compiler: gcc-2.7.2-psx */ /* decomp-assembler: maspsx-2.79-div */ "
           "/* decomp-cflags: -mno-split-addresses */ int f(void);")
    net._compiler_for(_proj(tmp_path), src, {"name": "f", "vram": VRAM, "unit": ""})
    compiler, args = seen["maspsx_config"]
    assert compiler["toolchain"] == "gcc-2.7.2-psx"
    assert compiler["flags"] == ["-O2", "-G8", "-mno-split-addresses"]  # the game's flags kept, the comment's added
    assert args == (("--aspsx-version=2.79", "--expand-div"),)


def test_gnu_modern_keeps_the_projects_small_data_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(network, "variant_compiler_config", lambda root, cfg: CompilerConfig(
        cmd=["cc"], asm_cmd=("as", "-EL", "-G0", "-o", "{obj}", "{src}"), asm_pass="psyq-move-addu"))
    net = object.__new__(Network)
    cc = net._compiler_for(_proj(tmp_path), "/* decomp-assembler: gnu-modern */ int f(void);",
                           {"name": "f", "vram": VRAM, "unit": ""})
    assert cc.asm_cmd == ("as", "-EL", "-G8", "-o", "{obj}", "{src}") and cc.asm_pass == "psyq-move-addu"


def test_without_comments_the_project_compiler_is_used(tmp_path, built):
    net, seen = built
    net._compiler_for(_proj(tmp_path), "int f(void);", {"name": "f", "vram": VRAM, "unit": ""})
    assert set(seen) == {"compiler_config"} and seen["compiler_config"][0]["toolchain"] == "gcc-2.8.1-psx"
    net._compiler_for(_proj(tmp_path), "/* decomp-assembler: gnu-old */ /* decomp-compiler: ido-7.1 */ int f(void);",
                      {"name": "f", "vram": VRAM, "unit": ""})
    assert seen["compiler_config"][0] == {"toolchain": "gcc-2.8.1-psx", "flags": ["-O2", "-G8"]}  # another CPU's: ignored


def test_a_source_with_comments_is_never_rebuilt_by_a_variant(tmp_path, monkeypatch):
    net = object.__new__(Network)
    monkeypatch.setattr(Network, "_variant_configs", lambda self, proj: pytest.fail("variant tried"))
    assert net._variant_attempt({"name": "f", "vram": VRAM, "unit": ""}, _proj(tmp_path),
                                "/* decomp-cflags: -fforce-addr */ int f(void);", {"header": ""}, None) is None


def test_a_source_with_only_unknown_comments_still_gets_the_variants(tmp_path, monkeypatch):
    net = object.__new__(Network)
    tried = []
    monkeypatch.setattr(Network, "_variant_configs", lambda self, proj: tried.append(1) or [])
    net._variant_attempt({"name": "f", "vram": VRAM, "unit": ""}, _proj(tmp_path),
                         "/* decomp-assembler: gnu-old-full */ int f(void);", {"header": ""}, None)
    assert tried == [1]


def test_maspsx_config_runs_maspsx_then_the_modern_assembler(tmp_path, monkeypatch):
    maspsx_dir, asm_dir = tmp_path / "maspsx", tmp_path / "binutils"
    maspsx_dir.mkdir()
    asm_dir.mkdir()
    (asm_dir / "as").write_text("")
    monkeypatch.setattr(project, "compiler_config", lambda root, cfg: CompilerConfig(cmd=["gcc", "-c"]))
    monkeypatch.setattr(toolchains, "require",
                        lambda tid, linux=None: {"maspsx": maspsx_dir, "binutils-mips": asm_dir}[tid])
    cfg = json.loads(_proj(tmp_path)["config"])
    v = project.maspsx_config(tmp_path, cfg, ("--aspsx-version=2.79",))
    assert v.asm_pp_cmd[1:] == (str(maspsx_dir / "maspsx.py"), "--aspsx-version=2.79", "-G8")
    assert v.asm_cmd == (str(asm_dir / "as"), "-EL", "-G0", "-o", "{obj}", "{src}") and v.cache is False


needs_psx = pytest.mark.skipif(
    any(toolchains.installed(t) is None for t in ("gcc-2.8.1-psx", "maspsx", "binutils-mips")),
    reason="needs gcc-2.8.1-psx, maspsx and binutils-mips")


def _insns(obj: bytes, name: str) -> list[int]:
    from decompnet.elfobj import extract_function

    fn = extract_function(obj, name)
    return [int.from_bytes(fn.code[i:i + 4], "little") for i in range(0, len(fn.code), 4)]


@needs_psx
def test_the_assembler_comment_really_changes_the_build(tmp_path):
    """Loads right before a call and before the return: the period GNU assembler puts a load-delay nop before the
    `jal` and before `jr $ra`; maspsx emulating ASPSX 2.79 doesn't, like the game. Honouring the comment is what
    makes such a function matchable here."""
    from decompnet.compiler import compile_source

    proj = {"root": str(tmp_path), "config": json.dumps({
        "kind": "rom", "big_endian": False, "compiler": {"toolchain": "gcc-2.8.1-psx", "flags": ["-O2", "-G0"]}})}
    net = object.__new__(Network)
    fn = {"name": "f", "vram": VRAM, "unit": ""}
    body = "extern int g(int); extern int D; int f(int *p) { int s = *p; g(s); return D + s; }"
    plain = compile_source(net._compiler_for(proj, body, fn), "", body)
    src = "/* decomp-assembler: maspsx-2.79 */\n" + body
    sony = compile_source(net._compiler_for(proj, src, fn), "", src)
    assert plain.ok and sony.ok, (plain.log, sony.log)
    plain, sony = _insns(plain.obj, "f"), _insns(sony.obj, "f")
    assert plain.count(0) == 2 and sony.count(0) == 0
    assert [w for w in plain if w] == sony
