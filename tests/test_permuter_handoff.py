"""The built-in permuter as the default finisher for near misses.

An agent's compile that lands a near miss (under 5% of the function's score, the same number of
instructions, only register/stack/order differences) hands the function to the permuter with that attempt
as the seed, and the result tells the agent to release and move on. Queued hand-offs come before the
permuter's own scan; while the permuter has a function, strategy claims skip it and the brief says so.
Stub-based: no compiler needed."""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest

from decompnet import scheduler, scoring, validity
from decompnet.app.engine import RomNetwork
from decompnet.app.permute import Handoff, State
from decompnet.network import NetworkError, near_miss
from decompnet.scheduler import ResourceBudget

N_INSNS = 100  # max score 10000: the hand-off line is 500
NEAR_SOURCE = "int func_001(void) { int t = 1; return t; }"
FAR_SOURCE = "int func_001(void) { return 1 + 1; }"
BIG_SOURCE = "int func_001(void) { return 2; }"


def same_rows(n: int, marks: dict[int, str]) -> list:
    """`n` target instructions against `n` of ours, with the given rows marked different."""
    rows = []
    for i in range(n):
        m = marks.get(i, " ")
        rows.append((m, f"insn{i}", f"insn{i}" if m == " " else f"insn{i}'"))
    return rows


NEAR_ROWS = same_rows(N_INSNS, {3: "r", 7: "s", 20: "o"})
FAR_ROWS = same_rows(N_INSNS, {3: "r"}) + [(">", "", "addiu $sp, $sp, -8")]  # one instruction too many
DIFFS = {NEAR_SOURCE: scoring.DiffResult(40, N_INSNS * 100, NEAR_ROWS, False),
         FAR_SOURCE: scoring.DiffResult(40, N_INSNS * 100, FAR_ROWS, False),
         BIG_SOURCE: scoring.DiffResult(900, N_INSNS * 100, NEAR_ROWS, False)}  # r/o/s only, but 9% off


class FakeResult:
    ok, log, obj = True, "", b"\x03\xe0\x00\x08"


@pytest.fixture
def net(tmp_path, monkeypatch):
    n = RomNetwork(tmp_path / "app.db", tmp_path)
    with n.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'fixture', 'fixture', 'hand-off fixture', 'mips', 0, ?, '{\"kind\": \"rom\"}')",
                   (str(tmp_path),))
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, 'int func_001(void);', NULL, 'fixture', 0, '')")
        for i in (1, 2, 3):
            db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, seed, status, "
                       "attempts, abandons) VALUES(?,1,?,?,?,?,'[]','nop','/* m2c */','open',0,0)",
                       (i, f"func_{i:03d}", N_INSNS * 4, N_INSNS, b"\0" * (N_INSNS * 4)))  # target: scored, not hash-only; a seed: the ROM brief drafts none
    # the judge, stubbed: a compile always succeeds and the diff is looked up by source
    monkeypatch.setattr(n, "_compile", lambda proj, header, source, fn=None: FakeResult())
    monkeypatch.setattr(n, "_evaluate", lambda fn, proj, obj: (SimpleNamespace(fingerprint=lambda: "fp"), None, []))
    monkeypatch.setattr(n, "_target_obj", lambda fn, proj: None)
    monkeypatch.setattr(n, "_worth_variant", lambda verdict, d: False)
    monkeypatch.setattr(n, "_validity", lambda fn, source: validity.Validity(ok=True))
    monkeypatch.setattr(n, "_extra_brief", lambda fn, proj: [])  # the ROM brief's map/xref block needs a real import
    monkeypatch.setattr(scoring, "diff", lambda target, cand, **kw: DIFFS[kw.get("source") or CURRENT[0]])
    # the permuter's thread is not started: a running permuter is simulated by its job state
    started = []
    monkeypatch.setattr(n.permuter, "start", lambda slug: started.append(slug) or n.permuter.jobs.setdefault(slug, State()))
    n.permuter_started = started
    return n


CURRENT = [NEAR_SOURCE]  # the source the stubbed diff answers for (attempt() does not pass it to scoring.diff)


def attempt(net, agent, name: str, source: str) -> str:
    CURRENT[0] = source
    return net.compile_and_diff(agent, name, source, "fixture")


def agent(net, name):
    return net.authenticate(net.register_agent(name))


def test_near_miss_is_small_same_count_and_register_stack_order_only():
    assert near_miss(DIFFS[NEAR_SOURCE], N_INSNS)
    assert not near_miss(DIFFS[FAR_SOURCE], N_INSNS)  # one instruction too many
    assert not near_miss(DIFFS[BIG_SOURCE], N_INSNS)  # 9% off
    operand = scoring.DiffResult(40, N_INSNS * 100, same_rows(N_INSNS, {3: "i"}), False)
    assert not near_miss(operand, N_INSNS)  # a different operand is not the permuter's kind of difference
    assert not near_miss(scoring.DiffResult(0, N_INSNS * 100, same_rows(N_INSNS, {}), True), N_INSNS)  # a match
    assert not near_miss(DIFFS[NEAR_SOURCE], 401)  # giants are left to agents
    assert not near_miss(DIFFS[NEAR_SOURCE], 0)


def test_a_near_miss_compile_result_hands_the_function_to_the_permuter(net):
    alice = agent(net, "alice")
    net.claim(alice, "fixture", "func_001")
    out = attempt(net, alice, "func_001", NEAR_SOURCE)
    assert "HANDED TO THE PERMUTER" in out and "Next action: RELEASE func_001" in out, out
    assert "No better in your last" not in out  # the hand-off is the next action; no stall nudge on top
    hand = net.permuter.queue[1]
    assert isinstance(hand, Handoff) and hand.source == NEAR_SOURCE and hand.score == 40 and hand.agent == "alice"
    assert net.permuter_started == ["fixture"]  # the hand-off starts (or wakes) the project's permuter
    assert net.permuter.holds(1) == "queued"
    proj = net.project_row("fixture")
    assert net.permuter.queued(proj) == []  # alice still holds the claim: it waits for her release
    net.release(alice, "func_001", "near miss, left to the permuter", "fixture")
    ready = net.permuter.queued(proj)
    assert [f["name"] for f in ready] == ["func_001"]
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    assert net.permuter._seed_source(fn) == (NEAR_SOURCE, 40)  # the run starts from the agent's attempt


def test_queued_hand_offs_come_before_the_permuters_own_scan(net):
    alice = agent(net, "alice")
    with net.store.tx() as db:  # func_002 is the closest near miss the scan would pick first
        db.execute("UPDATE functions SET best_score=1, best_source='int func_002(void) { return 2; }' WHERE id=2")
    net.claim(alice, "fixture", "func_001")
    attempt(net, alice, "func_001", NEAR_SOURCE)
    net.release(alice, "func_001", "", "fixture")
    proj = net.project_row("fixture")
    assert [f["name"] for f in net.permuter.candidates(proj)][0] == "func_002"
    assert [f["name"] for f in net.permuter.round(proj)] == ["func_001", "func_002"]


def test_a_far_off_result_is_not_handed_off(net):
    alice = agent(net, "alice")
    net.claim(alice, "fixture", "func_001")
    out = attempt(net, alice, "func_001", FAR_SOURCE)  # same score, but the instruction count differs
    assert "HANDED TO THE PERMUTER" not in out
    assert net.permuter.queue == {} and net.permuter_started == []
    out = attempt(net, alice, "func_001", BIG_SOURCE)  # r/o/s only, but far from the line
    assert "HANDED TO THE PERMUTER" not in out
    assert net.permuter.queue == {}


def test_brief_says_the_permuter_has_it_and_strategy_claims_skip_it(net):
    alice, bob = agent(net, "alice"), agent(net, "bob")
    net.claim(alice, "fixture", "func_001")
    attempt(net, alice, "func_001", NEAR_SOURCE)
    net.release(alice, "func_001", "", "fixture")
    assert net.permuter.running("fixture")
    # the brief says the permuter has it, seeded with alice's attempt
    brief = net.get_function("func_001", "fixture")
    assert "PERMUTER: func_001 is queued" in brief and "alice's attempt at score 40" in brief, brief
    # strategy claims (even "closest", which loves near misses) skip it while a permuter is running
    with net.store.tx() as db:
        db.execute("UPDATE functions SET best_score=40, best_source=? WHERE id=1", (NEAR_SOURCE,))
    assert net.claim_row(bob, "fixture", strategy="closest")["name"] != "func_001"
    net.release(bob, net.store.one("SELECT f.name FROM claims c JOIN functions f ON f.id=c.function_id "
                                   "WHERE c.agent_id=?", bob.id)["name"], "", "fixture")
    # while a worker permutes it, the brief says so
    net.permuter.active[1] = ("func_001", 1)
    assert "permuting func_001 right now" in net.get_function("func_001", "fixture")
    net.permuter.active.clear()
    # by name an agent can still take it (it stays in line for when they let go)
    carol = agent(net, "carol")
    assert "PERMUTER: func_001 is queued" in net.claim(carol, "fixture", "func_001")
    net.release(carol, "func_001", "", "fixture")
    # with no permuter running nothing is held back from strategy claims, and the brief says so
    net.permuter.jobs.clear()
    assert "no permuter is running" in net.get_function("func_001", "fixture")
    dave = agent(net, "dave")
    with net.store.tx() as db:  # make func_001 the only sensible pick
        db.execute("UPDATE functions SET status='matched' WHERE id IN (2, 3)")
    assert net.claim_row(dave, "fixture", strategy="closest")["name"] == "func_001"


def test_claiming_by_name_while_the_permuter_holds_the_lease_says_so(net):
    permuter, _ = net.local_agent("permuter")
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    assert net.permuter._hold(permuter, fn)
    with pytest.raises(NetworkError, match="being permuted right now"):
        net.claim(agent(net, "alice"), "fixture", "func_001")


def test_a_hand_off_wakes_the_idle_loop(net):
    st = State()
    net.permuter._wake.clear()
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    threading.Timer(0.2, lambda: net.permuter.enqueue(fn, NEAR_SOURCE, 40, "alice")).start()
    t0 = time.monotonic()
    net.permuter._wait(st, 10)
    assert time.monotonic() - t0 < 5


def test_permuter_runs_on_about_a_quarter_of_the_cores():
    cpu = os.cpu_count() or 4
    assert ResourceBudget().limits["permuter"] == max(1, cpu // 4)
    assert scheduler.BUDGET.limits["permuter"] >= 1
    assert net_workers() == max(1, scheduler.BUDGET.limits["permuter"])


def net_workers() -> int:
    from decompnet.app.permute import Permuter
    return Permuter.workers()


def test_a_room_member_permutes_only_its_own_hand_offs_and_shares_them(net, monkeypatch):
    """A copy that joined the project from a friend's room still finishes what its own agents hand over; the
    project-wide scan stays the host's, and results go to the host like an agent's: a match to confirm, a better
    attempt as a hint."""
    from decompnet.app import permute

    calls = []
    alice = agent(net, "alice")
    with net.store.tx() as db:  # a near miss the host's scan would pick: not this copy's business
        db.execute("UPDATE functions SET best_score=1, best_source='int func_002(void) { return 2; }' WHERE id=2")
    net.claim(alice, "fixture", "func_001")
    attempt(net, alice, "func_001", NEAR_SOURCE)
    net.release(alice, "func_001", "", "fixture")
    proj = net.project_row("fixture")
    # from here the copy is a room member (a claim or compile would go through the room's host)
    net.links["fixture"] = SimpleNamespace(
        attempt=lambda agent, name, source: calls.append(("match", name, source)) or {"matched": True})
    monkeypatch.setattr(net, "_report", lambda link, agent, name, source, score: calls.append(("best", name, score)))
    assert net.permuter.member("fixture")
    assert [f["name"] for f in net.permuter.round(proj)] == ["func_001"]
    monkeypatch.setattr(permute.threading, "Thread", lambda *a, **kw: SimpleNamespace(start=lambda: None))
    assert isinstance(type(net.permuter).start(net.permuter, "fixture"), State)  # no longer refused for a member
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    net.permuter._share(alice, proj, fn, "int func_001(void) { return 1; }", 0, True)
    net.permuter._share(alice, proj, fn, NEAR_SOURCE, 12, False)
    assert calls == [("match", "func_001", "int func_001(void) { return 1; }"), ("best", "func_001", 12)]
    del net.links["fixture"]
    assert not net.permuter.member("fixture")
    assert [f["name"] for f in net.permuter.round(proj)] == ["func_001", "func_002"]  # the host's copy scans
