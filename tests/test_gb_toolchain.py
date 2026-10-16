"""The Game Boy C toolchain lane: SDCC/GBDK-2020 → SM83 bytes → the fork's scoring.

The compile tests are skipped when the toolchain isn't installed (it is a download, per platform —
see toolchains.toml); the registry and decoder tests always run.
"""

from __future__ import annotations

import pytest

from decompnet import scoring
from decompnet import toolchains as tc
from decompnet.elfobj import FunctionObj, extract_function
from decompnet.rom import gb_build, sm83

needs_toolchain = pytest.mark.skipif(not gb_build.available(),
                                     reason="the Game Boy C toolchain (gbdk-2020) is not installed")


# ------------------------------------------------------------------ registry

def test_the_gb_toolchain_is_registered_and_pinned():
    t = tc.get("gbdk-2020")
    assert t.arch == "sm83"
    assert t.platform.startswith("Game Boy")
    assert t.kind == "compiler"
    platforms = {p for part in t.parts for p in part.get("platforms", [])}
    assert {"macos-arm64", "macos-x86_64", "linux-x86_64", "linux-aarch64", "windows-x86_64"} <= platforms
    assert all(part.get("sha256") for part in t.parts), "every download must be pinned"


# ------------------------------------------------------------------ the build lane

@needs_toolchain
def test_compiles_a_leaf_function_to_exact_sm83():
    funcs = gb_build.build_source("void f(void) { }")
    assert funcs["f"].code == bytes.fromhex("c9")  # ret
    zero = gb_build.build_source("typedef unsigned char u8;\nu8 f(void) { return 0; }")
    assert zero["f"].code == bytes.fromhex("afc9")  # xor a, a · ret
    eleven = gb_build.build_source("typedef unsigned char u8;\nu8 f(void) { return 0x11; }")
    assert eleven["f"].code == bytes.fromhex("3e11c9")  # ld a, $11 · ret


@needs_toolchain
def test_symbols_resolve_to_the_roms_own_addresses():
    """Absolute mode: the caller supplies the project's symbol table and the candidate's call carries
    the ROM's real address (which is what byte-identical matching needs)."""
    funcs = gb_build.build_source("void g(void);\nvoid f(void) { g(); }", symbols={"g": 0x4006})
    code = funcs["f"].code
    assert code[1:3] == bytes.fromhex("0640"), f"expected the ROM address 0x4006 in {code.hex()}"
    assert code[0] in (0xC3, 0xCD)  # jp (tail call) or call
    # an unknown symbol is not silently assembled as zero
    with pytest.raises(Exception):
        gb_build.build_source("void g(void);\nvoid f(void) { g(); }")


@needs_toolchain
def test_payload_round_trips_into_a_function_object():
    payload = gb_build.build_payload("typedef unsigned char u8;\nu8 f(void) { return 0x11; }", name="f")
    assert payload.startswith(gb_build.PAYLOAD_MAGIC)
    fn = extract_function(payload, "f")
    assert fn is not None and fn.arch == "sm83" and fn.code == bytes.fromhex("3e11c9")


@needs_toolchain
def test_a_reporting_header_and_includes_are_accepted(tmp_path):
    fn = gb_build.compile_function("void f(void) { }", "f", context_header="typedef unsigned char u8;")
    assert fn.code == b"\xc9"


# ------------------------------------------------------------------ scoring (no toolchain needed)

def test_scoring_reads_sm83_instructions_without_capstone():
    insns = list(scoring._decoder("sm83", 0).disasm(bytes.fromhex("3e11c9cd0640"), 0x100))
    assert [(i.mnemonic, i.op_str) for i in insns] == [("ld", "a, $11"), ("ret", ""), ("call", "$4006")]
    assert all(i.id for i in insns)
    assert insns[2].group(None) is True and insns[0].group(None) is False
    assert insns[2].size == 3 and insns[2].address == 0x103  # after ld a,$11 (2) and ret (1)
    assert insns[2].bytes == bytes.fromhex("cd0640")


def test_scoring_matches_identical_sm83_code_and_reports_a_difference():
    target = FunctionObj("func_000b58", "sm83", False, bytes.fromhex("c9"), [])
    same = FunctionObj("f", "sm83", False, bytes.fromhex("c9"), [])
    assert scoring.diff(target, same).score == 0
    other = FunctionObj("f", "sm83", False, bytes.fromhex("afc9"), [])
    assert scoring.diff(target, other).score > 0


def test_the_decoder_covers_the_instruction_set_scoring_will_see():
    # every opcode the assembler can emit must decode to a non-empty mnemonic, or a diff would
    # silently compare garbage
    for op in range(0x100):
        for op2 in (None, 0x00, 0x37):
            code = bytes([op]) if op2 is None else bytes([0xCB, op]) if op == 0xCB else bytes([op, op2])
            text, ln = sm83.decode(code, 0)
            assert text and ln >= 1


@needs_toolchain
def test_the_whole_app_path_matches_a_real_game_boy_function(tmp_path):
    """Import -> claim -> diff -> MATCH, through the same engine code the app runs, on a synthetic
    cartridge whose function body is exactly `ret` (what C `void f(void) {}` compiles to)."""
    from decompnet.app.engine import RomNetwork
    from tests.test_gb import make_rom

    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    rom = tmp_path / "game.gb"
    rom.write_bytes(make_rom(size=0x8000, code=b"\xc9"))       # one `ret` at 0x150
    slug = net.import_rom(rom, confirmed=True, log=lambda *_: None)
    proj = net.store.one("SELECT * FROM projects WHERE slug=?", slug)
    cfg = __import__("json").loads(proj["config"])
    assert cfg["arch"] == "sm83" and cfg["compiler"]["toolchain"] == gb_build.TOOLCHAIN
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND target=? AND status='open'",
                       proj["id"], b"\xc9")
    assert fn is not None, "a one-instruction function must be claimable"
    agent = net.authenticate(net.register_agent("gb-tester"))
    net.claim(agent, function=fn["name"])
    out = net.compile_and_diff(agent, fn["name"], f"void {fn['name']}(void) {{ }}\n")
    assert "MATCH" in out, out
    assert net.store.one("SELECT status FROM functions WHERE id=?", fn["id"])["status"] == "matched"
    assert net.store.one("SELECT matched_agent_id FROM functions WHERE id=?", fn["id"])["matched_agent_id"] == agent.id


@needs_toolchain
def test_the_lane_refuses_a_candidate_that_does_not_define_the_function(tmp_path):
    """A candidate must define the function the app is judging — the failure is reported, not scored."""
    with pytest.raises(Exception):
        gb_build.build_payload("void something_else(void) { }", name="func_000b58")


@needs_toolchain
@pytest.mark.parametrize("code, c_source, what", [
    (bytes.fromhex("ea0021c9"), "typedef unsigned char u8;\nvoid {n}(u8 v) {{ *(volatile u8 *)0x2100 = v; }}\n",
     "a sound-register write: ld ($2100), a"),
    (bytes.fromhex("e0f2c9"), "typedef unsigned char u8;\nvoid {n}(u8 v) {{ *(volatile u8 *)0xFFF2 = v; }}\n",
     "an HRAM write: ldh ($ff00+$f2), a"),
])
def test_real_game_shapes_match_through_the_app(tmp_path, code, c_source, what):
    """Non-trivial Game Boy code, not just stubs: the shapes the lane must reproduce are volatile
    register writes — the idiom the brief teaches — and they must match through the whole app path."""
    from decompnet.app.engine import RomNetwork
    from tests.test_gb import make_rom

    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    rom = tmp_path / "game.gb"
    rom.write_bytes(make_rom(size=0x8000, code=code))
    slug = net.import_rom(rom, confirmed=True, log=lambda *_: None)
    proj = net.store.one("SELECT * FROM projects WHERE slug=?", slug)
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND target=?", proj["id"], code)
    assert fn is not None, f"expected the {what} function to be discovered"
    agent = net.authenticate(net.register_agent("gb-shape"))
    net.claim(agent, function=fn["name"])
    out = net.compile_and_diff(agent, fn["name"], c_source.format(n=fn["name"]))
    assert "MATCH" in out, f"{what}: {out}"


@needs_toolchain
def test_the_lane_enforces_the_source_policy(tmp_path):
    """Inline assembly is a loophole on a platform where targets can be one instruction: `__asm("nop")`
    reproduces them without any C. The Game Boy lane must apply the same source policy as every other."""
    from decompnet.compiler import SourceRejected

    for bad in ('void f(void) __naked {\n    __asm\n        nop\n    __endasm;\n}',
                'void f(void) { __asm("nop"); }',
                'void f(void) { __attribute__((naked)) ; }'):
        with pytest.raises(SourceRejected):
            gb_build.build_source(bad)
    # and a normal C candidate still compiles
    assert gb_build.build_source("void f(void) { }")["f"].code == b"\xc9"
