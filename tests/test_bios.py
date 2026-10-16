"""BIOS calls (bios.py): the stubs a game calls the console's BIOS through hold no game code and no C compiles to
them, so they're set aside like hand-written assembly, named, shown as BIOS, and a person can hand one back."""

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from decompnet.bios import reason
from decompnet.app.engine import RomNetwork

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "src/decompnet/app/static/demo/demo-quest-64.z64"


def le(*words: int) -> bytes:
    return b"".join(struct.pack("<I", w) for w in words)


def le16(*halves: int) -> bytes:
    return b"".join(struct.pack("<H", h) for h in halves)


# From a real PS1 game (SLUS-01363): li t2, 0xA0; jr t2; li t1, 0x3F (printf), and its padding
PRINTF = le(0x240A00A0, 0x01400008, 0x2409003F)
# PSY-Q's EnterCriticalSection: li a0, 1; syscall; jr ra; nop
ENTER_CRITICAL = le(0x24040001, 0x0000000C, 0x03E00008, 0)


def test_ps1_bios_stubs():
    assert reason(PRINTF, "psx").startswith("PS1 BIOS call A0:3F (printf) wrapper")
    assert "B0:38 (exit)" in reason(le(0x240A00B0, 0x01400008, 0x24090038, 0), "psx")
    assert "C0:0A (ChangeClearRCnt)" in reason(le(0x240A00C0, 0x01400008, 0x2409000A), "psx")
    # t1 set before the jump instead of in its delay slot, and ori instead of addiu
    assert "A0:34 (free)" in reason(le(0x340A00A0, 0x24090034, 0x01400008, 0), "psx")
    # the splitter ran two stubs together (SLUS-01363's func_8004AB2C)
    two = reason(le(0x240A00A0, 0x01400008, 0x24090024, 0, 0x240A00A0, 0x01400008, 0x24090027), "psx")
    assert two.startswith("2 PS1 BIOS call wrappers run together: A0:24 (strstr), A0:27 (bcopy)")
    assert "kernel call 0x01 (EnterCriticalSection)" in reason(ENTER_CRITICAL, "psx")


def test_ps1_code_that_does_more_is_left_alone():
    """Only exact stubs: game code taken for one would never be worked on."""
    assert reason(PRINTF + le(0x24020001), "psx") is None  # a stub, then something else
    assert reason(le(0x240A00A0, 0x01400008, 0x24090099, 0x03E00008, 0), "psx") is None
    assert reason(le(0x240A00A0, 0x01400008, 0x00000000), "psx") is None  # no call number
    assert reason(le(0x240A00D0, 0x01400008, 0x2409003F), "psx") is None  # not a BIOS table
    assert reason(le(0x240A00A0, 0x01400008, 0x24020001), "psx") is None  # delay slot doing other work
    # a library function that calls B0:57 (GetB0Table) through jalr and does work of its own (func_8004B65C)
    lib = le(0x3C01800A, 0xAC3F9A30, 0x24090057, 0x240A00B0, 0x0140F809, 0, 0x8C42016C, 0x03E00008, 0)
    assert reason(lib, "psx") is None
    compiled = le(0x27BDFFE8, 0xAFBF0010, 0x0C000000, 0, 0x8FBF0010, 0x03E00008, 0x27BD0018)
    assert reason(compiled, "psx") is None
    assert reason(le(0x03E00008, 0), "psx") is None and reason(b"", "psx") is None
    assert reason(le(0x24040001, 0x0000000C, 0x03E00008, 0x24020001), "psx") is None  # return delay slot does work


def test_ps2_kernel_calls():
    assert reason(le(0x24030020, 0x0000000C, 0x03E00008, 0), "ps2").startswith(
        "PS2 kernel call 0x20 (CreateThread) wrapper (syscall)")
    assert "-0x43 (an interrupt-context call)" in reason(le(0x2403FFBD, 0x0000000C, 0x03E00008, 0), "ps2")  # li v1, -0x43
    assert reason(le(0x24030020, 0x0000000C, 0x03E00008, 0), "psx") is None  # the PS1 passes it in a0
    assert reason(le(0x24030020, 0x24020001, 0x0000000C, 0x03E00008, 0), "ps2") is None
    assert reason(le(0x240A00A0, 0x01400008, 0x2409003F), "ps2") is None  # the PS1's tables aren't the PS2's


def test_gba_and_ds_swi_stubs():
    assert reason(le16(0xDF0B, 0x4770), "gba").startswith("GBA BIOS call swi 0x0B (CpuSet) wrapper")
    assert "swi 0x05 (VBlankIntrWait)" in reason(le16(0xDF05, 0x4770, 0x46C0, 0), "gba")  # with padding
    assert "swi 0x0C (CpuFastSet)" in reason(le(0xEF0C0000, 0xE12FFF1E), "gba")  # ARM mode
    assert reason(le16(0xDF09, 0x4770), "nds").startswith("DS BIOS call swi 0x09 (Div)")
    assert reason(le16(0x2001, 0xDF0B, 0x4770), "gba") is None  # movs r0, #1 first: does more
    assert reason(le16(0x1C08, 0x4770), "gba") is None  # adds r0, r1, #0; bx lr: ordinary code


def test_platforms_without_bios_calls():
    assert reason(PRINTF, "n64") is None and reason(PRINTF, "gc") is None
    assert reason(bytes([0xC9]), "gbc") is None and reason(bytes([0xC9]), "gb") is None


@pytest.mark.skipif(shutil.which("arm-none-eabi-as") is None, reason="needs an ARM assembler")
def test_ds_objects_say_which_code_is_a_bios_call(tmp_path):
    from decompnet.app.twins import object_prints

    (tmp_path / "x.s").write_text("""
    .text
    .thumb
    .globl SVC_Div
    .type SVC_Div, %function
    .thumb_func
SVC_Div:
    swi 9
    bx lr
    .size SVC_Div, .-SVC_Div
    .globl add
    .type add, %function
    .thumb_func
add:
    add r0, r0, r1
    bx lr
    .size add, .-add
""")
    subprocess.run(["arm-none-eabi-as", "-mcpu=arm946e-s", "-o", str(tmp_path / "x.o"), str(tmp_path / "x.s")], check=True)
    prints = object_prints((tmp_path / "x.o").read_bytes(), "nds")
    assert prints["SVC_Div"].bios.startswith("DS BIOS call swi 0x09 (Div)") and prints["add"].bios == ""


@pytest.fixture()
def ps1(tmp_path):
    """The demo game, as a PS1 project (little-endian MIPS), with BIOS stubs added: one open, one already set aside
    as assembly, one matched (by someone who wrote it in asm long ago: never unmatched)."""
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    net.on_match = lambda *a: None
    slug = net.import_rom(DEMO, confirmed=True, log=lambda *_: None)
    proj = net.project_row(slug)
    cfg = net.cfg(proj) | {"platform": "psx"}
    with net.store.tx() as db:
        db.execute("UPDATE projects SET big_endian=0, config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
        for r in db.execute("SELECT id, target FROM functions WHERE project_id=?", (proj["id"],)).fetchall():
            t = r["target"] or b""  # the same code, little-endian
            db.execute("UPDATE functions SET target=? WHERE id=?",
                       (b"".join(t[i:i + 4][::-1] for i in range(0, len(t), 4)), r["id"]))
        for name, status, code in (("func_80401000", "open", PRINTF), ("func_80401010", "asm", ENTER_CRITICAL),
                                   ("func_80401020", "matched", le(0x240A00B0, 0x01400008, 0x24090038))):
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,unit,vram,status) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)", (proj["id"], name, len(code), len(code) // 4, code, "[]",
                                                      f"glabel {name}\n", "", int(name[5:], 16), status))
    return net, slug


def test_bios_calls_are_set_aside_shown_and_handed_back(ps1):
    from decompnet.app import tools

    net, slug = ps1
    proj = net.project_row(slug)
    before = {r["name"]: r["status"] for r in net.store.all("SELECT name, status FROM functions WHERE project_id=?", proj["id"])}
    sig = net.map_signature(slug)
    net.overview(slug)  # opening the project sets them aside (projects imported before the detector get it too)
    row = net._function("func_80401000", slug)
    assert row["status"] == "asm" and json.loads(row["meta"])["bios"].startswith("PS1 BIOS call A0:3F (printf)")
    assert "EnterCriticalSection" in json.loads(net._function("func_80401010", slug)["meta"])["bios"]
    assert net._function("func_80401020", slug)["status"] == "matched"  # a match is never undone
    after = {r["name"]: r["status"] for r in net.store.all("SELECT name, status FROM functions WHERE project_id=?", proj["id"])}
    assert {n for n in after if after[n] != before[n]} == {"func_80401000"}  # the game's own code: untouched
    assert net.map_signature(slug) != sig
    shown = {f["name"]: f["status"] for f in net.function_map(slug)}
    assert shown["func_80401000"] == shown["func_80401010"] == "bios" and shown["func_80401020"] == "matched"
    assert net.function_detail(slug, "func_80401000")["bios"].startswith("PS1 BIOS call A0:3F")

    # agents aren't handed one; asked for by name, they're told why and get the next function
    agent, _ = net.local_agent("bios-asker")
    out = tools.call(net, agent, slug, "claim_function", {"function": "func_80401000"})
    assert "is BIOS code, a PS1 BIOS call A0:3F (printf) wrapper" in out and "## CLAIMED" in out
    held = net.store.one("SELECT f.name FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=?",
                         agent.id)["name"]
    assert held not in ("func_80401000", "func_80401010")
    net.release(agent, held, "", slug)

    # a person says it's game code: agents get it, and the detector leaves it alone from then on
    assert net.give_to_agents(slug, "func_80401000")["for_agents"]
    assert net._function("func_80401000", slug)["status"] == "open"
    assert net.twins.set_aside_asm(net.project_row(slug)) == 0
    assert net._function("func_80401000", slug)["status"] == "open"
    assert next(f for f in net.function_map(slug) if f["name"] == "func_80401000")["status"] == "open"
    with pytest.raises(Exception):
        net.give_to_agents(slug, "func_80401020")  # matched: not set aside, nothing to give
    net.give_to_agents(slug, "func_80401000", give=False)
    row = net._function("func_80401000", slug)
    assert row["status"] == "asm" and "for_agents" not in json.loads(row["meta"])


def test_the_autopilot_skips_bios_calls(ps1, monkeypatch):
    """The autopilot's list is taken before its twins pass sets BIOS calls aside: it drops them after."""
    import threading

    net, slug = ps1
    proj = net.project_row(slug)
    monkeypatch.setattr(net.twins, "run", lambda slug, job=None, only=None: job)  # no compiling here
    ids = [r["id"] for r in net.store.all("SELECT id FROM functions WHERE project_id=? AND status='open'", proj["id"])]
    stub = net._function("func_80401000", slug)["id"]
    assert stub in ids
    tried: list[int] = []
    state = {"state": "running", "total": len(ids), "tried": 0, "matched": 0, "near": 0, "twins": 0,
             "phase": "twins", "stop": threading.Event()}
    net._autopilot_run(slug, ids, state, 1, try_one=lambda agent, proj, fid: tried.append(fid) or "fail")
    assert tried and stub not in tried and net._function("func_80401000", slug)["status"] == "asm"

