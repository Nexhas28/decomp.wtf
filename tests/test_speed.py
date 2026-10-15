"""What makes agents fast: free matches from identical code (twins), batches of small functions judged in one
call, and tools that don't waste an agent's turn (names resolved, next function instead of an error, a nudge to
move on). On Demo Quest 64 (projects/demo-rom), with twins made the way games have them: the same code at
another address, referring to other data."""

import json
import re
import shutil
import subprocess
import sys

import pytest
from elftools.elf.elffile import ELFFile

from decompnet import toolchains
from decompnet.app import tools
from decompnet.app.engine import RomNetwork
from decompnet.app.twins import Print, apply, renames
from decompnet.forks import csource
from decompnet.rom import match

from .test_export import DEMOS, FUNCTIONS, GAME

pytestmark = pytest.mark.skipif(toolchains.installed("gcc-2.7.2-kmc") is None or not shutil.which("ld.lld"),
                                reason="needs gcc-2.7.2-kmc and ld.lld to build the demo ROM")


def test_renames_follow_the_relocations():
    a = Print("k", ["g_player", "g_player+0x1C", "rng_next", ".rodata"], 40)
    b = Print("k", ["D_80045000", "D_80045000+0x1C", "func_80001234", ".rodata"], 40)
    assert renames(a, b, "f", "func_80009000") == {"f": "func_80009000", "g_player": "D_80045000",
                                                    "rng_next": "func_80001234"}
    assert renames(a, Print("k", ["x", "y+0x20", "z", ".rodata"], 40), "f", "g") is None  # another field: other code
    assert renames(Print("k", ["a", "a"], 8), Print("k", ["b", "c"], 8), "f", "g") is None  # one name, two things
    assert apply("void f(void) { a = b; b = a2; }", {"a": "b", "b": "c"}) == "void f(void) { b = c; c = a2; }"


@pytest.fixture(scope="module")
def game(tmp_path_factory):
    rom, elf, _, build = DEMOS["n64"]
    if not elf.exists():
        subprocess.run([sys.executable, str(build)], check=True)
    work = tmp_path_factory.mktemp("speed")
    net = RomNetwork(work / "app.db", work)
    net.on_match = lambda *a: None  # the tests run the twins themselves
    slug = net.import_rom(rom, confirmed=True, log=lambda *_: None)
    agent, _ = net.local_agent("agent")
    syms = {s.name: s["st_value"] for s in ELFFile(open(elf, "rb")).get_section_by_name(".symtab").iter_symbols()}
    pid = net.project_row(slug)["id"]
    for name in FUNCTIONS:  # as test_export does: split what the splitter merged, name what's known
        row = net.store.one("SELECT name, vram FROM functions WHERE project_id=? AND vram<=? AND vram+size>?",
                            pid, syms[name], syms[name])
        if row and row["vram"] != syms[name]:
            net.split_function(agent, slug, row["name"], f"0x{syms[name]:08X}", name)
    header = "\n".join(line for line in (GAME / "game.h").read_text().splitlines() if not line.startswith("#include")
                       and not re.match(r"typedef \w+( \w+)? [su]\d+;|typedef \w+ f\d+;", line))
    assert net.edit_context(agent, slug, "", header, "the game's types").startswith("✅")
    table = net.symbols(pid)
    for name in FUNCTIONS + ["g_player", "g_entities", "g_score", "g_frame_count", "g_rng_state"]:
        current, offset = table.name_at(syms[name])
        if not offset and current != name:
            net.rename_symbol(agent, slug, current, name)
            table = net.symbols(pid)
    return net, slug, syms


def twin(net, slug: str, of: str, at: int, data_to: dict[int, int] | None = None) -> str:
    """A copy of a function at another address, its data references moved (the bytes say so too): a twin."""
    proj = net.project_row(slug)
    fn = net._function(of, slug)
    relocs = json.loads(fn["relocs"])
    code = bytearray(fn["target"])
    moved = []
    for off, rtype, addr in relocs:
        new = addr
        for base, to in (data_to or {}).items():
            if base <= addr < base + 0x100:
                new = to + (addr - base)
        if new != addr:
            word = int.from_bytes(code[off:off + 4], "big")
            mask = 0x03FFFFFF if rtype == match.R_MIPS_26 else 0xFFFF
            word = (word & ~mask) | (match.encode(rtype, new) & mask)
            code[off:off + 4] = word.to_bytes(4, "big")
        moved.append([off, rtype, new])
    name = f"func_{at:08X}"
    with net.store.tx() as db:
        db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (proj["id"], name, fn["size"], fn["n_insns"], bytes(code),
                                                    json.dumps(moved), fn["asm"], None, fn["unit"], at, fn["rom_offset"]))
        db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                   (proj["id"], name, at, fn["size"], "func"))
    net._syms.pop(proj["id"], None)
    net.twins.forget(proj)
    return name


def test_a_match_matches_its_twins(game):
    """player_take_damage at two more addresses: one the same, one writing another Player (the copy's data
    references point elsewhere). Once the original matches, both match by themselves, with the right names."""
    net, slug, syms = game
    agent, _ = net.local_agent("agent")
    same = twin(net, slug, "player_take_damage", 0x80400000)
    other = twin(net, slug, "player_take_damage", 0x80400100, {syms["g_player"]: 0x80047000})
    proj = net.project_row(slug)
    assert net.twins.group_sizes(proj)[net._function(same, slug)["id"]] == 3
    assert net.twins.update_boosts(proj) >= 1  # one of the three goes first, the others last
    boosts = sorted(net._function(n, slug)["boost"] for n in ("player_take_damage", same, other))
    assert boosts == [-1, -1, 2]
    source = csource.get((GAME / "game.c").read_text(), "player_take_damage")
    assert net.attempt(agent, "player_take_damage", source, slug)["matched"]
    assert "Identical code" in net.get_function(same, slug)  # an agent who looks is shown it
    job = net.twins.run(slug)
    assert job.matched == 2
    for name, data in ((same, "g_player"), (other, "D_80047000")):
        row = net._function(name, slug)
        assert row["status"] == "matched" and data in row["matched_source"] and f"{name}(" in row["matched_source"]


def test_a_batch_of_small_functions_in_one_call(game):
    """claim_function(count=3): three small neighbours in one brief; compile_and_diff with all three names judges
    each, in one call."""
    net, slug, _ = game
    agent, _ = net.local_agent("batcher")
    brief = tools.call(net, agent, slug, "claim_function", {"count": 3, "strategy": "easiest"})
    assert brief.startswith("## CLAIMED 3 small functions"), brief[:300]
    held = [r["name"] for r in net.store.all("SELECT f.name FROM claims c JOIN functions f ON f.id=c.function_id "
                                             "WHERE c.agent_id=?", agent.id)]
    assert len(held) == 3
    source = "\n".join(f"void {n}(void) {{ }}" for n in held)
    out = tools.call(net, agent, slug, "compile_and_diff", {"function": ", ".join(held), "source": source})
    assert re.match(r"\d of 3 matched", out) and all(f"### {n}" in out for n in held), out[:500]


def test_turns_not_wasted(game):
    """A claim of a matched function is the next one; a near name says what it may be; four compiles without
    progress get a nudge to move on."""
    net, slug, _ = game
    agent, _ = net.local_agent("asker")
    out = tools.call(net, agent, slug, "claim_function", {"function": "player_take_damage"})
    assert "already matched" in out and "## CLAIMED" in out
    assert "Did you mean" in tools.call(net, agent, slug, "get_function", {"function": "player_take_damag"})
    fn = net.store.one("SELECT f.name FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=?", agent.id)["name"]
    for k in range(5):
        out = net.compile_and_diff(agent, fn, f"void {fn}(void) {{ volatile int x = {k}; }}", slug)
    assert "No better in your last" in out


def test_the_permuter_finishes_a_near_miss(game):
    """A near miss (a matching source with one rewrite that changes its code: statements swapped, a cast) is
    left to the permuter, which finds its way back to a match on its own."""
    import random

    from decompnet.app.permute import State
    from decompnet.permuter import mutate

    net, slug, _ = game
    agent, _ = net.local_agent("agent")
    source = csource.get((GAME / "game.c").read_text(), "entity_update_physics")
    assert net.attempt(agent, "entity_update_physics", source, slug)["matched"]
    fn = net._function("entity_update_physics", slug)
    near = None
    for seed in range(200):  # a variant that compiles and is close, but not the same code
        variant = mutate(source, random.Random(seed))
        res = net.try_quietly(agent, net.project_row(slug), {**fn, "status": "open"}, variant) if variant else None
        if res and res["score"] and res["score"] <= fn["n_insns"] * 100 * 0.06:
            near = (variant, res["score"])
            break
    assert near, "no near-miss variant to start from"
    with net.store.tx() as db:  # as if agents had left it there
        db.execute("UPDATE functions SET status='open', matched_source=NULL, best_score=?, best_source=? WHERE id=?",
                   (near[1], near[0], fn["id"]))
    proj = net.project_row(slug)
    assert [r["name"] for r in net.permuter.candidates(proj)][:1] == ["entity_update_physics"]
    permuter, _ = net.local_agent("permuter")
    st = State()
    net.permuter._permute(permuter, proj, net._function("entity_update_physics", slug), st)
    assert st.matched == 1 and net._function("entity_update_physics", slug)["status"] == "matched", st
