"""The auto-fixer (fixer.py) and its repairs (boundary.py): cuts moved without losing a byte, set-asides, permuter
hand-offs, only for its own sessions and only on a project's own copy; and which notes start it."""

from __future__ import annotations

import json
import struct

import pytest

from decompnet.app import boundary, fixer, tools
from decompnet.app.engine import RomNetwork
from decompnet.handwritten import mips_reason
from decompnet.network import NetworkError
from decompnet.rom import split

ADDIU_SP_M18, SW_RA, LW_RA, JR_RA = 0x27BDFFE8, 0xAFBF0010, 0x8FBF0010, 0x03E00008
ADDIU_SP_18, NOP, MOVE_V0_A0 = 0x27BD0018, 0x00000000, 0x00801021
BASE, ROM = 0x80001000, 0x800

# A ends on jr ra; its delay slot was cut off into B.            B is then a 3-instruction getter.
# D runs on into E, which is only D's epilogue (torn in two).    F is an ordinary function after them.
LAYOUT = [
    ("func_80001000", [ADDIU_SP_M18, SW_RA, LW_RA, JR_RA]),
    ("func_80001010", [ADDIU_SP_18, MOVE_V0_A0, JR_RA, NOP]),
    ("func_80001020", [ADDIU_SP_M18, SW_RA, MOVE_V0_A0]),
    ("func_8000102C", [LW_RA, JR_RA, ADDIU_SP_18]),
    ("func_80001038", [MOVE_V0_A0, JR_RA, NOP]),
]
MNEMONIC = {ADDIU_SP_M18: ("addiu", "$sp, $sp, -0x18"), SW_RA: ("sw", "$ra, 0x10($sp)"),
            LW_RA: ("lw", "$ra, 0x10($sp)"), JR_RA: ("jr", "$ra"), ADDIU_SP_18: ("addiu", "$sp, $sp, 0x18"),
            NOP: ("nop", ""), MOVE_V0_A0: ("addu", "$v0, $a0, $zero")}


def rom_bytes() -> bytes:
    return b"".join(struct.pack("<I", w) for _, ws in LAYOUT for w in ws)


@pytest.fixture
def net(tmp_path):
    return make(tmp_path / "app.db", tmp_path / "work")


def make(db_path, home):
    n = RomNetwork(db_path, home)
    cfg = {"kind": "rom", "platform": "psx", "arch": "mips", "big_endian": False, "gp": 0,
           "rom": {"sha1": "ab" * 20, "title": "GAME", "game_code": "SLUS-00000", "region": "USA"},
           "compiler": {"toolchain": "gcc-2.8.1-psx", "flags": ["-O2"]}}
    with n.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'game', 'game', '', 'mips', 0, '/tmp/game', ?)", (json.dumps(cfg),))
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, '', NULL, 'fixture', 0, '')")
        vram, rom = BASE, ROM
        for name, words in LAYOUT:
            lines = [split.AsmLine(rom + 4 * i, vram + 4 * i, w, *MNEMONIC[w]) for i, w in enumerate(words)]
            rf = split.RomFunction(name, vram, rom, "main", lines, None)
            code = b"".join(struct.pack("<I", w) for w in words)
            why = mips_reason(code, False, "psx")
            db.execute("INSERT INTO functions(project_id, name, size, n_insns, target, relocs, asm, status, unit, vram, "
                       "rom_offset, meta, best_source, best_score) VALUES(1,?,?,?,?,'[]',?,?,'',?,?,?,?,?)",
                       (name, 4 * len(words), len(words), code, RomNetwork._asm_source(rf), "asm" if why else "open",
                        vram, rom, json.dumps({"asm_code": why}) if why else None,
                        f"int {name}(int a) {{ return a; }}", 100))
            db.execute("INSERT INTO symbols(project_id, name, address, size, kind) VALUES(1,?,?,?,'func')",
                       (name, vram, 4 * len(words)))
            vram += 4 * len(words)
            rom += 4 * len(words)
    n.compile_and_diff = lambda agent, function, source, project=None: f"(judged {function})"
    return n


@pytest.fixture
def fixer_agent(net):
    agent, _ = net.local_agent("fixer-1")
    net.fixer_ids.add(agent.id)
    return agent


def layout(net):
    return [(r["name"], r["vram"], r["size"], r["status"]) for r in
            net.store.all("SELECT * FROM functions WHERE project_id=1 ORDER BY vram")]


def all_code(net) -> bytes:
    return b"".join(r["target"] for r in net.store.all("SELECT target FROM functions WHERE project_id=1 ORDER BY vram"))


def test_a_delay_slot_goes_back_to_its_function(net, fixer_agent):
    out = net.fix_boundary(fixer_agent, "game", "func_80001000", "func_80001010", "0x80001014", "slot of A's jr")
    assert out.startswith("✅")
    assert layout(net)[:2] == [("func_80001000", 0x80001000, 20, "open"), ("func_80001014", 0x80001014, 12, "open")]
    assert all_code(net) == rom_bytes()  # every byte still there, in one function each
    syms = net.symbols(1)
    assert "func_80001010" not in syms.by_name and syms.by_name["func_80001014"].size == 12
    assert "judged func_80001014" in out  # the saved best is judged again


def test_a_torn_function_is_joined_and_given_back_to_agents(net, fixer_agent):
    assert layout(net)[2][3] == "asm"  # it ran on into its epilogue: set aside for that
    with net.store.tx() as db:
        db.execute("INSERT INTO notes(project_id, function_id, agent_id, text, created_at) "
                   "SELECT 1, id, NULL, 'epilogue-only fragment, needs merge', 0 FROM functions WHERE name='func_8000102C'")
    out = net.fix_boundary(fixer_agent, "game", "func_80001020", "func_8000102C", "0x80001038", "epilogue of D")
    assert out.startswith("✅ joined")
    assert ("func_80001020", 0x80001020, 24, "open") in layout(net)
    assert all(name != "func_8000102C" for name, *_ in layout(net))
    assert all_code(net) == rom_bytes()
    kept = net.store.one("SELECT f.name FROM notes n JOIN functions f ON f.id=n.function_id")
    assert kept["name"] == "func_80001020"  # what agents learned stays, on the whole function


def test_a_cut_that_strands_a_delay_slot_is_refused(net, fixer_agent):
    with pytest.raises(NetworkError, match="delay slot"):  # A would still end on jr ra
        net.fix_boundary(fixer_agent, "game", "func_80001000", "func_80001010", "0x80001010", "x")
    with pytest.raises(NetworkError, match="instruction boundary"):
        net.fix_boundary(fixer_agent, "game", "func_80001000", "func_80001010", "0x80001012", "x")
    assert all_code(net) == rom_bytes()


def test_a_start_other_code_calls_stays(net, fixer_agent):
    with net.store.tx() as db:  # F calls B: a function really starts at B's address
        db.execute("UPDATE functions SET asm=asm || char(10) || '/* 0 0 0 */  jal        func_80001010' "
                   "WHERE name='func_80001038'")
    with pytest.raises(NetworkError, match="calls or points at"):
        net.fix_boundary(fixer_agent, "game", "func_80001000", "func_80001010", "0x80001014", "x")


def test_only_neighbours_unmatched_and_unclaimed(net, fixer_agent):
    with pytest.raises(NetworkError, match="aren't neighbours"):
        net.fix_boundary(fixer_agent, "game", "func_80001000", "func_80001020", "0x80001014", "x")
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched' WHERE name='func_80001010'")
    with pytest.raises(NetworkError, match="matched"):
        net.fix_boundary(fixer_agent, "game", "func_80001000", "func_80001010", "0x80001014", "x")
    other, _ = net.local_agent("claude-1")
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='open' WHERE name='func_80001010'")
        db.execute("INSERT INTO claims VALUES((SELECT id FROM functions WHERE name='func_80001010'),?,0,9e12)", (other.id,))
    with pytest.raises(NetworkError, match="claimed"):
        net.fix_boundary(fixer_agent, "game", "func_80001000", "func_80001010", "0x80001014", "x")


def test_only_the_fixer_and_only_on_its_own_copy(net, fixer_agent):
    helper, _ = net.local_agent("claude-1")
    with pytest.raises(NetworkError, match="only the auto-fixer"):
        net.fix_boundary(helper, "game", "func_80001000", "func_80001010", "0x80001014", "x")
    with pytest.raises(NetworkError, match="only the auto-fixer"):
        net.set_aside_function(helper, "game", "func_80001038", "x")
    assert "fix_boundary" not in tools.tool_names(False)
    assert "fix_boundary" in tools.tool_names(False, fixer=True)
    assert "fix_boundary" not in tools.tool_names(True, fixer=True)  # (forks have their own files to edit)
    net.links["game"] = object()
    with pytest.raises(NetworkError, match="host"):
        net.fix_boundary(fixer_agent, "game", "func_80001000", "func_80001010", "0x80001014", "x")


def test_set_aside_respects_a_persons_say(net, fixer_agent):
    assert net.set_aside_function(fixer_agent, "game", "func_80001038", "BIOS stub").startswith("✅")
    assert net._function("func_80001038", "game")["status"] == "asm"
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='open', meta='{\"for_agents\": true}' WHERE name='func_80001038'")
    with pytest.raises(NetworkError, match="by hand"):
        net.set_aside_function(fixer_agent, "game", "func_80001038", "BIOS stub")


def test_the_permuter_gets_a_wall_released_function(net, fixer_agent):
    fn = net._function("func_80001038", "game")
    with net.store.tx() as db:  # a "wall" release spent its permuter budget
        db.execute("INSERT INTO search_budgets(function_id, epoch, policy_version, turns_used, max_turns, cooldown_until, "
                   "reset_reason, last_at) VALUES(?, 'wall', 1, 4, 4, 9e12, 'wall:x', 0)", (fn["id"],))
    started = []
    net.permuter.start = lambda slug: started.append(slug)
    out = net.queue_for_permuter(fixer_agent, "game", "func_80001038", "regalloc wall")
    assert out.startswith("✅") and started == ["game"]
    assert net.permuter.holds(fn["id"]) == "queued"
    assert net.store.one("SELECT 1 FROM search_budgets WHERE function_id=?", fn["id"]) is None


def test_a_member_follows_the_hosts_layout(net, tmp_path):
    """The host joined D+E and moved A|B's cut: the member's copy joins, then the split pass cuts where the host's
    are."""
    host = [{"name": "func_80001000", "vram": 0x80001000, "size": 20},
            {"name": "func_80001014", "vram": 0x80001014, "size": 12},
            {"name": "func_80001020", "vram": 0x80001020, "size": 24},
            {"name": "func_80001038", "vram": 0x80001038, "size": 12}]
    net.apply_remote_layout("game", host, [])
    assert [(n, v, s) for n, v, s, _ in layout(net)] == [(h["name"], h["vram"], h["size"]) for h in host]
    assert all_code(net) == rom_bytes()


def test_a_hosts_repair_reaches_the_room(tmp_path):
    """The host moves a cut and joins a torn function; on its next sync the member's copy has the same layout."""
    from starlette.testclient import TestClient

    from decompnet.app import collab

    host = make(tmp_path / "h.db", tmp_path / "h")
    room = host.rooms["game"] = collab.Room(host, "game")
    client = TestClient(collab.host_app(host, host.rooms), base_url="http://192.168.1.20:6481")
    joined = client.post("/collab/join?room=game", json={"invite": room.open_invite(), "name": "kai",
                                                         "sha1": "ab" * 20}).json()
    guest = make(tmp_path / "g.db", tmp_path / "g")
    link = guest.link_project("game", {"url": "http://192.168.1.20:6481", "room": "game", "token": joined["token"],
                                       "member": joined["name"]}, client=client, start=False)
    link.sync()
    agent, _ = host.local_agent("fixer-1")
    host.fixer_ids.add(agent.id)
    host.fix_boundary(agent, "game", "func_80001000", "func_80001010", "0x80001014", "delay slot")
    host.fix_boundary(agent, "game", "func_80001020", "func_8000102C", "0x80001038", "epilogue")
    mine, _ = guest.local_agent("fixer-1")
    guest.fixer_ids.add(mine.id)
    with pytest.raises(NetworkError, match="host"):  # the member can't repair its own copy
        guest.fix_boundary(mine, "game", "func_80001000", "func_80001010", "0x80001014", "x")
    link.sync()
    assert [x[:3] for x in layout(guest)] == [x[:3] for x in layout(host)]
    assert all_code(guest) == rom_bytes()


def test_hand_written_code_keeps_ra_in_a_global():
    lui_at, sw_ra_at = 0x3C01800A, 0xAC3F1234  # lui at,0x800A; sw ra,0x1234(at)
    assert "return address" in mips_reason(struct.pack("<3I", lui_at, sw_ra_at, JR_RA) + b"\0" * 4, False, "psx")
    assert mips_reason(struct.pack("<4I", ADDIU_SP_M18, SW_RA, LW_RA, JR_RA) + struct.pack("<I", ADDIU_SP_18),
                       False, "psx") is None


@pytest.mark.parametrize("text, kinds", [
    ("SPARKDOG_20: boundary wall floor 100 (3/3 match, delay nop belongs to EB8). Needs merge. Releasing.", {"boundary"}),
    ("SPARKDOG_19: 3-insn epilogue fragment, needs lead-side merge. Released without attempt.", {"boundary"}),
    ("SPARKDOG_12: hand-written BIOS exception-hook installer (ra spilled to global). Not C-expressible.", {"set_aside"}),
    ("SPARKDOG_13: Same s6/s7 swap wall as twin; all compilers swept. Needs permuter.", {"permuter"}),
    ("Merged the two bp temps into one outer pointer -> score 3830 (new best).", set()),
    ("split the multiply into `s32 t = D * 0x18` (matches ROM operand order)", set()),
])
def test_which_notes_ask_for_the_fixer(text, kinds):
    assert fixer.flags(text) == kinds


def test_flagged_notes_start_one_session_with_the_list(net, monkeypatch):
    monkeypatch.setattr(fixer, "DEBOUNCE", 0.01)
    started = []

    class Manager:
        def start_fixer(self, slug, items, model, effort, on_done):  # noqa: ARG002
            started.append((slug, [i["function"] for i in items], model, effort))
            return "fixer-1"

    fx = fixer.Fixer(net, Manager())
    helper, _ = net.local_agent("claude-1")
    fx.observe("game", "func_80001010", "leading addiu is 1000's jr delay slot; needs merge", helper)
    fx.observe("game", "func_80001038", "a plain note about types", helper)
    import time
    for _ in range(100):
        if started:
            break
        time.sleep(0.02)
    assert started == [("game", ["func_80001010"], "claude-sonnet-5-5", "medium")]
    assert net._function("func_80001010", "game")["meta"] is None  # looked at only once the session got through
    fx.observe("game", "func_80001000", "mis-split, needs merge", helper)  # one session at a time: it waits
    assert fx.status("game") == {"running": "fixer-1", "waiting": ["func_80001000"], "working_on": ["func_80001010"]}
    fixer_agent, _ = net.local_agent("fixer-1")
    net.fixer_ids.add(fixer_agent.id)
    fx.observe("game", "func_80001038", "auto-fixer: mis-split? no, left alone", fixer_agent)  # its own notes: nothing
    assert fx.status("game")["waiting"] == ["func_80001000"]
    fx._done("game", ok=True)
    assert json.loads(net._function("func_80001010", "game")["meta"])["fixer_at"] > 0


def test_only_mips_projects_start_the_fixer(net):
    with net.store.tx() as db:
        db.execute("UPDATE projects SET arch='sm83', config=json_set(config, '$.arch', 'sm83', '$.platform', 'gbc')")
    fx = fixer.Fixer(net, None)
    fx.observe("game", "func_80001010", "fragment, needs merge")
    assert fx.status("game")["waiting"] == [] and fx.scan("game") == 0
    assert "N64, PlayStation" in fx.why_not("game")
    with pytest.raises(ValueError, match="PlayStation"):
        fx.run_now("game")


def test_giants_are_not_flagged_for_the_permuter(net):
    fn = dict(net._function("func_80001038", "game"))
    assert fixer.wanted(fn, {"permuter"}) == {"permuter"}
    assert fixer.wanted(fn | {"n_insns": 413}, {"permuter"}) == set()
    assert fixer.wanted(fn | {"status": "matched"}, {"boundary"}) == set()


def test_run_now_starts_at_once_even_when_switched_off(net):
    started = []

    class Manager:
        def start_fixer(self, slug, items, model, effort, on_done):  # noqa: ARG002
            started.append([i["function"] for i in items])
            return "fixer-1"

    with net.store.tx() as db:
        db.execute("INSERT INTO notes(project_id, function_id, agent_id, text, created_at) SELECT 1, id, NULL, "
                   "'mis-split: needs merge', 0 FROM functions WHERE name='func_80001010'")
    fx = fixer.Fixer(net, Manager(), config=lambda: {"enabled": False, "model": "claude-opus-5-5"})
    assert fx.scan("game") == 0  # switched off: helpers starting don't run it
    out = fx.run_now("game")
    assert started == [["func_80001010"]] and out["running"] == "fixer-1" and out["flagged"] == 1
    net.links["game"] = object()
    assert "host" in fx.why_not("game")
