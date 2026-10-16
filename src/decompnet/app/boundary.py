"""Repairs only a project's own app may make: moving where two neighbouring functions meet, setting a function aside
as hand-written, handing a near miss to the permuter. The auto-fixer (fixer.py) does them; agents can't.

A cut in the wrong place makes both pieces unmatchable: a function whose last delay slot went to its neighbour, a
neighbour that starts with someone else's instruction, a function torn in two. `recut` moves the cut: the two
pieces' instructions are joined and cut again at the new address (or kept as one), so every byte of the game stays
exactly where it was and belongs to exactly one function. Only on a project's own copy (in a team room, the host's):
a member's copy follows the host's layout on its next sync (follow_layout).
"""

from __future__ import annotations

import json
import re
import time

import rabbitizer

from ..handwritten import mips_reason
from ..network import NetworkError
from ..rom import split
from ..rom.symbols import AUTO_NAME, Symbol


def _addr(at: str) -> int:
    m = re.match(r"^(?:0x)?([0-9A-Fa-f]{2,8})$", str(at).strip())
    if not m:
        raise NetworkError("`at` must be an address like 0x80001234")
    return int(m.group(1), 16)


def _own_copy(net, slug: str) -> None:
    if slug in getattr(net, "links", {}):
        raise NetworkError("in a team room only the host's app changes function boundaries and what agents get")


def _mips_rom(net, proj) -> None:
    cfg = net.cfg(proj)
    if not net.is_rom(proj) or (cfg.get("arch") or proj["arch"] or "mips").lower() != "mips":
        raise NetworkError("this works on MIPS ROM projects (N64, PlayStation, PlayStation 2)")


def _free(net, fn, agent_id: int | None = None) -> None:
    """Nobody may be working on a function whose code is about to change."""
    if fn["status"] == "matched":
        raise NetworkError(f"{fn['name']} is matched: matched functions are never changed")
    claim = net.store.one("SELECT agent_id FROM claims WHERE function_id=? AND expires_at>?", fn["id"], time.time())
    if claim is not None and claim["agent_id"] != agent_id:
        raise NetworkError(f"{fn['name']} is claimed right now: try again once it's released")
    permuter = getattr(net, "permuter", None)
    if permuter is not None and permuter.holds(fn["id"]) == "permuting":
        raise NetworkError(f"the permuter is working on {fn['name']} right now: try again later")


def _has_delay_slot(word: int) -> bool:
    return rabbitizer.Instruction(word).hasDelaySlot()


def _status(net, proj, code: bytes, status: str, meta: dict) -> tuple[str, dict]:
    """asm or open for a piece's new code: a cut in the wrong place is often the only reason a function was set
    aside ("runs on into the next function"). A BIOS call, or a person's give-back, keeps its say."""
    if status == "matched" or meta.get("bios") or meta.get("for_agents") or meta.get("fixer_aside"):
        return status, meta
    why = mips_reason(code, bool(proj["big_endian"]), net.cfg(proj).get("platform", ""))
    meta = {k: v for k, v in meta.items() if k != "asm_code"}
    if why:
        return "asm", meta | {"asm_code": why}
    return "open", meta


def _cut(net, proj, a, b, at: int) -> list[tuple]:
    """The pieces (row, RomFunction) after moving the a|b cut to `at` (at == the end of b: one function)."""
    ra = net._parse_asm(a["name"], a["asm"], a["unit"])
    rb = net._parse_asm(b["name"], b["asm"], b["unit"])
    lines = ra.lines + rb.lines
    for x, y in zip(lines, lines[1:]):
        if y.vram != x.vram + 4 or y.rom != x.rom + 4:
            raise NetworkError(f"{a['name']} and {b['name']} have a gap or overlap between them: not two halves "
                               f"of one stretch of code")
    end = lines[-1].vram + 4
    if not a["vram"] < at <= end or (at - a["vram"]) % 4:
        raise NetworkError(f"0x{at:08X} is not an instruction boundary between 0x{a['vram']:08X} and 0x{end:08X}")
    labels = ra.labels | rb.labels | ({b["vram"]} if at != b["vram"] else set())
    k = (at - a["vram"]) // 4
    head = split.RomFunction(a["name"], a["vram"], a["rom_offset"], ra.segment, lines[:k], ra.asm_path,
                             {x for x in labels if x < at})
    if at == end:
        return [(a, head)]
    name = b["name"]
    if at != b["vram"] and AUTO_NAME.fullmatch(name):
        name = f"func_{at:08X}"
    tail = split.RomFunction(name, at, lines[k].rom, rb.segment, lines[k:], rb.asm_path, {x for x in labels if x >= at})
    return [(a, head), (b, tail)]


def recut(net, proj, a, b, at: int, agent_id: int | None, reason: str = "", check: bool = True) -> list[str]:
    """Move the cut between adjacent functions a and b (a first) to `at`, or join them (at == b's end). Returns
    the resulting function names. `check`: the auto-fixer's rules (nobody working on them, no piece ending on a
    jump whose delay slot is in the other). A room member following its host passes check=False: the host
    checked."""
    if a["vram"] > b["vram"]:
        a, b = b, a
    if a["unit"] != b["unit"] or a["rom_offset"] is None or b["rom_offset"] is None \
            or a["rom_offset"] + (a["size"] or 0) != b["rom_offset"] or a["vram"] + (a["size"] or 0) != b["vram"]:
        raise NetworkError(f"{a['name']} and {b['name']} aren't neighbours in the ROM: no cut between them to move")
    for fn in (a, b):
        if net.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=? AND vram=?",
                         proj["id"], fn["vram"])["n"] > 1:
            raise NetworkError(f"several overlays share {fn['name']}'s address: boundaries there are left alone")
    if check:
        _free(net, a, agent_id)
        _free(net, b, agent_id)
    elif a["status"] == "matched" or b["status"] == "matched":
        raise NetworkError("matched functions are never changed")
    pieces = _cut(net, proj, a, b, at)
    if check:
        for i, (_, rf) in enumerate(pieces):
            if not _has_delay_slot(rf.lines[-1].word):
                continue
            if i + 1 < len(pieces):
                raise NetworkError(f"{rf.name} would end on a jump or branch whose delay slot (the next "
                                   f"instruction) is in {pieces[i + 1][1].name}: cut one instruction later")
            raise NetworkError(f"{rf.name} would end on a jump or branch whose delay slot is the first instruction "
                               f"of the function after it: move that cut first (fix_boundary({b['name']}, <the next "
                               f"function>, 0x{rf.vram + rf.size + 4:08X})), then join")
        joined = len(pieces) == 1  # (a jumping into b is a's own code once they're one function)
        if at != b["vram"] and (callers := _callers(net, proj, b, {b["id"]} | ({a["id"]} if joined else set()))):
            raise NetworkError(f"other code calls or points at {b['name']}'s address ({', '.join(callers[:3])}), so a "
                               f"function really starts there: its cut stays")
    big = bool(proj["big_endian"])
    order = "big" if big else "little"
    before = b"".join(ln.word.to_bytes(4, order) for _, rf in pieces for ln in rf.lines)
    whole = b"".join(ln.word.to_bytes(4, order) for ln in
                     net._parse_asm(a["name"], a["asm"], a["unit"]).lines + net._parse_asm(b["name"], b["asm"], b["unit"]).lines)
    if before != whole:  # (can't happen: the pieces are the same lines) never let bytes go missing
        raise NetworkError("the pieces wouldn't hold exactly the original bytes: nothing changed")
    try:
        return _apply(net, proj, a, b, at, pieces, agent_id, reason)
    finally:  # (also when it failed half-way: the next read rebuilds them from the database)
        for cache in ("_xrefs", "_ops", "_syms"):
            getattr(net, cache, {}).pop(proj["id"], None)


def _callers(net, proj, fn, skip: set[int]) -> list[str]:
    """The functions (but those in `skip`) whose assembly names `fn` (a jal, a j, an address taken)."""
    word = re.compile(rf"\b{re.escape(fn['name'])}\b")
    rows = [r for r in net.store.all("SELECT id, name, asm FROM functions WHERE project_id=? AND asm LIKE ?",
                                     proj["id"], f"%{fn['name']}%") if r["id"] not in skip]
    return [r["name"] for r in rows if any(word.search(ln) for ln in (r["asm"] or "").splitlines()
                                           if not ln.lstrip().startswith(("glabel", "endlabel")))]


def _apply(net, proj, a, b, at: int, pieces: list, agent_id: int | None, reason: str) -> list[str]:
    syms = net.symbols(proj["id"])
    rows = {r["id"]: r for r in (a, b)}
    names = [rf.name for _, rf in pieces]
    if len(pieces) == 2 and pieces[1][1].name != b["name"] and pieces[1][1].name in syms.by_name:
        raise NetworkError(f"`{pieces[1][1].name}` is already taken")
    for row, rf in pieces:
        old = syms.by_name.pop(row["name"], None)
        if old is not None:
            syms.by_addr.pop(old.address, None)
        syms.add(Symbol(rf.name, rf.vram, rf.size, "func"))
    if len(pieces) == 1:
        gone = syms.by_name.pop(b["name"], None)
        if gone is not None:
            syms.by_addr.pop(gone.address, None)
    targets = [(row, rf, net._make_target(proj, rf, syms)) for row, rf in pieces]
    detail = (f"{a['name']} + {b['name']} joined" if len(pieces) == 1
              else f"{a['name']} / {names[1]} now meet at 0x{at:08X}")
    with net.store.tx() as db:
        for row, rf, t in targets:
            meta = json.loads(rows[row["id"]]["meta"] or "{}") if rows[row["id"]]["meta"] else {}
            status, meta = _status(net, proj, t.code, rows[row["id"]]["status"], meta)
            best = rows[row["id"]]["best_source"]
            if best and rf.name != row["name"]:  # the attempt defines the function: under its new name
                best = re.sub(rf"\b{re.escape(row['name'])}\b", rf.name, best)
            db.execute("UPDATE functions SET name=?, vram=?, rom_offset=?, size=?, n_insns=?, target=?, relocs=?, asm=?, "
                       "seed=NULL, best_score=NULL, best_source=?, status=?, meta=?, revision=revision+1 WHERE id=?",
                       (rf.name, rf.vram, rf.rom, rf.size, len(rf.lines), t.code, json.dumps(t.to_json()),
                        net._asm_source(rf), best, status, json.dumps(meta) if meta else None, row["id"]))
            db.execute("DELETE FROM symbols WHERE project_id=? AND name=?", (proj["id"], row["name"]))
            db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                       (proj["id"], rf.name, rf.vram, rf.size, "func"))
        if len(pieces) == 1:  # b is gone: what people learned about it stays, on the function it was part of
            db.execute("UPDATE notes SET function_id=? WHERE function_id=?", (a["id"], b["id"]))
            db.execute("DELETE FROM merge_proposals WHERE function_id=? OR continues_id=?", (b["id"], b["id"]))
            net._delete_function_rows(db, b["id"])
            db.execute("DELETE FROM functions WHERE id=?", (b["id"],))
            db.execute("DELETE FROM symbols WHERE project_id=? AND name=?", (proj["id"], b["name"]))
        for row in (a, b):  # walls and cooldowns were about the old cut
            db.execute("DELETE FROM function_walls WHERE function_id=?", (row["id"],))
            db.execute("DELETE FROM search_budgets WHERE function_id=?", (row["id"],))
        net._event(db, proj["id"], agent_id, "boundary", detail + (f": {reason.strip()[:200]}" if reason.strip() else ""))
    return names


def fix_boundary(net, agent, slug: str, first: str, second: str, at: str, reason: str) -> str:
    """The auto-fixer's tool: move the cut between two neighbouring functions, then judge their saved best
    attempts again (a function that was only off by its cut often matches straight away)."""
    _own_copy(net, slug)
    proj = net.project_row(slug)
    _mips_rom(net, proj)
    if not reason.strip():
        raise NetworkError("say why (the evidence): it goes in the project's history")
    a, b = net._function(first, slug), net._function(second, slug)
    names = recut(net, proj, a, b, _addr(at), agent.id, reason)
    out = [("✅ joined: " + names[0]) if len(names) == 1 else f"✅ the cut is now at {at}: {names[0]}, {names[1]}"]
    for name in names:
        fn = net._function(name, slug)
        out.append(f"- {name}: {fn['n_insns']} instructions, {fn['status']}"
                   + (f" ({json.loads(fn['meta'])['asm_code']})" if fn["status"] == "asm" and fn["meta"]
                      and json.loads(fn["meta"]).get("asm_code") else ""))
        if fn["status"] == "open" and fn["best_source"]:
            out.append(f"  its saved best attempt, judged again: {_rejudge(net, agent, slug, fn)}")
    return "\n".join(out)


def _rejudge(net, agent, slug: str, fn) -> str:
    try:
        text = net.compile_and_diff(agent, fn["name"], fn["best_source"], slug)
    except NetworkError as e:
        return f"not judged ({e})"
    finally:
        with net.store.tx() as db:  # the compile claimed it for the fixer: let it go
            db.execute("DELETE FROM claims WHERE function_id=? AND agent_id=?", (fn["id"], agent.id))
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:200]


def set_aside(net, agent, slug: str, function: str, reason: str) -> str:
    """The auto-fixer's tool: take a function no C compiles to (hand-written, a BIOS or kernel call) out of
    agents' way, like the detectors do. A person's "give it to agents" always wins."""
    _own_copy(net, slug)
    proj = net.project_row(slug)
    if not net.is_rom(proj):
        raise NetworkError("set_aside_function is for ROM projects")
    if not reason.strip():
        raise NetworkError("say why no C compiles to it")
    fn = net._function(function, slug)
    meta = json.loads(fn["meta"] or "{}") if fn["meta"] else {}
    if fn["status"] == "asm":
        return f"{fn['name']} is already set aside."
    if meta.get("for_agents"):
        raise NetworkError(f"a person gave {fn['name']} to agents by hand: it stays theirs")
    _free(net, fn, agent.id)
    meta["asm_code"] = f"set aside by the auto-fixer: {reason.strip()[:300]}"
    meta["fixer_aside"] = True
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='asm', meta=? WHERE id=? AND status='open'", (json.dumps(meta), fn["id"]))
        net._event(db, proj["id"], agent.id, "set_aside", f"{fn['name']} set aside by the auto-fixer")
    twins = getattr(net, "twins", None)
    if twins is not None and hasattr(twins, "forget"):
        twins.forget(proj)
    return f"✅ {fn['name']} is set aside as assembly: agents and the autopilot leave it alone."


def queue_permuter(net, agent, slug: str, function: str, reason: str = "") -> str:
    """The auto-fixer's tool: hand a function whose C is right but whose registers aren't to the permuter, with
    the best attempt as its starting point. An earlier "wall" release spent its permuter budget: that is reset."""
    _own_copy(net, slug)
    proj = net.project_row(slug)
    permuter = getattr(net, "permuter", None)
    if permuter is None:
        raise NetworkError("this project has no permuter")
    _mips_rom(net, proj)
    from .permute import MAX_INSNS

    fn = net._function(function, slug)
    if fn["status"] != "open":
        raise NetworkError(f"{fn['name']} is {fn['status']}: only open functions go to the permuter")
    if not fn["best_source"] or not fn["best_score"]:
        raise NetworkError(f"{fn['name']} has no scored attempt to start the permuter from")
    if (fn["n_insns"] or 0) > MAX_INSNS:
        raise NetworkError(f"{fn['name']} is too big for the permuter ({fn['n_insns']} instructions, at most {MAX_INSNS})")
    if (held := permuter.holds(fn["id"])):
        return f"{fn['name']} is already {held} by the permuter."
    with net.store.tx() as db:
        db.execute("DELETE FROM search_budgets WHERE function_id=?", (fn["id"],))
        net._event(db, proj["id"], agent.id, "permuter", f"{fn['name']} queued by the auto-fixer"
                   + (f": {reason.strip()[:200]}" if reason.strip() else ""))
    permuter.enqueue(fn, fn["best_source"], fn["best_score"], agent.name)
    try:
        permuter.start(slug)
    except Exception as e:  # queued either way: it runs when the permuter does
        return f"{fn['name']} is queued, but the permuter didn't start: {e}"
    state = permuter.holds(fn["id"])
    if not state:
        return f"⚠ {fn['name']} didn't stay in the permuter's queue (its search budget or size rules it out)."
    return f"✅ {fn['name']} is {state} by the permuter (from the best attempt, score {fn['best_score']})."


def follow_layout(net, proj, functions: list[dict]) -> int:
    """A room member's copy: join the local functions the host no longer has as functions of their own (the host
    joined them or moved a cut), so the split pass after it can cut them where the host's are. Returns how many
    were joined. Matched functions and addresses several overlays share are left alone."""
    host = {}
    for f in functions:
        host.setdefault(f["vram"], []).append(f)
    joined = 0
    for _ in range(len(functions) + 1):  # each pass joins at most one; stops when nothing is left to join
        rows = net.store.all("SELECT * FROM functions WHERE project_id=? ORDER BY vram, id", proj["id"])
        counts: dict[int, int] = {}
        for r in rows:
            counts[r["vram"]] = counts.get(r["vram"], 0) + 1
        done = True
        for prev, r in zip(rows, rows[1:]):
            if r["vram"] is None or prev["vram"] is None:
                continue
            if r["vram"] in host or counts[r["vram"]] > 1 or counts[prev["vram"]] > 1:
                continue
            if r["status"] == "matched" or prev["status"] == "matched" or prev["vram"] not in host:
                continue
            try:
                recut(net, proj, prev, r, r["vram"] + (r["size"] or 0), None, check=False)
            except NetworkError:
                continue
            joined += 1
            done = False
            break
        if done:
            break
    return joined
