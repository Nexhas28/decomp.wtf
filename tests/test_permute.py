"""Queue, lease and budget correctness for the background permuter (plan Q-01..Q-06).

These tests run without any compiler: they exercise the store/claim/budget logic directly.
"""

from __future__ import annotations

import threading
import time

import pytest

from decompnet.app.permute import MAX_TURNS, Permuter, State
from decompnet.network import Agent, Network


def make_project(net: Network, n_functions: int, near: float = 0.06) -> dict:
    """A synthetic project with `n_functions` open near-miss functions. Function i (1-based) has
    best_score = i, so the permuter's near-miss ordering (best_score / (n_insns * 100)) ranks
    lower numbers first."""
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'fixture', 'fixture', 'queue fixture', 'mips', 0, '/tmp/fixture', '{}')")
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, 'int func_xxx(void);', NULL, 'fixture', 0, '')")
        for i in range(1, n_functions + 1):
            db.execute(
                "INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, seed, status, "
                "best_score, best_source, best_agent_id, attempts, abandons) VALUES(?,1,?,?,100,NULL,'[]','',NULL,"
                "'open',?,?,NULL,0,0)",
                (i, f"func_{i:03d}", 400, i, f"/* best attempt {i} */\nint func_{i:03d}(void) {{ return {i}; }}"),
            )
    return net.store.one("SELECT * FROM projects WHERE id=1")


def exhaust(net: Network, ids: range, turns: int = MAX_TURNS) -> None:
    """Mark functions as having spent their permuter turn budget."""
    now = time.time()
    with net.store.tx() as db:
        for i in ids:
            db.execute("INSERT INTO search_budgets(function_id, turns_used, max_turns, last_at) VALUES(?,?,?,?)",
                       (i, turns, MAX_TURNS, now))


@pytest.fixture
def net(tmp_path):
    return Network(tmp_path / "queue.db")


def test_q01_exhausted_functions_cannot_hide_remaining_work(net):
    """The top 80 near-misses have spent their budget; the next 20 are eligible. The permuter must
    return those 20 instead of an empty queue (plan Q-01: the reviewed query applies LIMIT before
    the Python turn filter, so limit*4 exhausted rows hid every eligible function)."""
    proj = make_project(net, 100)
    exhaust(net, range(1, 81))
    perm = Permuter(net)
    todo = perm.candidates(proj, limit=20)
    assert [f["name"] for f in todo] == [f"func_{i:03d}" for i in range(81, 101)]


def test_q01b_ordering_is_deterministic_and_stable(net):
    """The same candidate window is returned on every call: a stable ordering with a tiebreak
    (plan P1.1 steps 6 and 8), so the pagination fix advances instead of re-querying page one."""
    proj = make_project(net, 100)
    exhaust(net, range(1, 81))
    perm = Permuter(net)
    first = [f["id"] for f in perm.candidates(proj, limit=20)]
    second = [f["id"] for f in perm.candidates(proj, limit=20)]
    assert first == second
    assert first == list(range(81, 101))


def test_q01c_budget_is_persisted_across_restarts(net):
    """Exhaustion survives a restart: a fresh Permuter over the same database still sees the same
    eligible set (plan P1.1 step 4 and Q-05: budgets are database state, not an in-memory dict)."""
    proj = make_project(net, 100)
    exhaust(net, range(1, 81))
    Permuter(net).candidates(proj, limit=20)
    fresh = Permuter(Network(net.store.conn.execute("PRAGMA database_list").fetchone()["file"]))
    todo = fresh.candidates(proj, limit=20)
    assert [f["id"] for f in todo] == list(range(81, 101))


def test_q01d_zero_or_unknown_instruction_counts_are_never_ranked(net):
    """Functions with n_insns 0/NULL must not appear in the near-miss ranking (plan P1.1 step 5:
    guard the score normalisation basis)."""
    proj = make_project(net, 5)
    with net.store.tx() as db:
        db.execute("UPDATE functions SET n_insns=0 WHERE id=1")
        db.execute("UPDATE functions SET n_insns=NULL WHERE id=2")
    perm = Permuter(net)
    ids = [f["id"] for f in perm.candidates(proj, limit=20)]
    assert 1 not in ids and 2 not in ids
    assert ids == [3, 4, 5]


def test_q02_claimed_functions_are_not_candidates(net):
    """An open function another agent holds a claim on must not be offered to the permuter
    (plan Q-02's prerequisite: only open, unclaimed candidates are selected)."""
    proj = make_project(net, 5)
    with net.store.tx() as db:
        db.execute("INSERT INTO agents(id, name, owner, token_hash) VALUES(1, 'worker', NULL, 't1')")
        db.execute("INSERT INTO claims(function_id, agent_id, claimed_at, expires_at) VALUES(1, 1, 0, 9e18)")
    perm = Permuter(net)
    ids = [f["id"] for f in perm.candidates(proj, limit=20)]
    assert 1 not in ids
    assert ids == [2, 3, 4, 5]


def test_q02b_two_workers_one_atomic_lease(net):
    """Of two workers racing to hold one function, exactly one lease is granted (plan Q-02)."""
    proj = make_project(net, 5)
    with net.store.tx() as db:
        db.execute("INSERT INTO agents(id, name, owner, token_hash) VALUES(1,'w1',NULL,'t1')")
        db.execute("INSERT INTO agents(id, name, owner, token_hash) VALUES(2,'w2',NULL,'t2')")
    fn = net.store.one("SELECT * FROM functions WHERE id=3")
    perm = Permuter(net)
    a1, a2 = Agent(1, "w1", None, True), Agent(2, "w2", None, True)
    barrier, results = threading.Barrier(2), []

    def hold(agent):
        barrier.wait()
        results.append(perm._hold(agent, fn))

    threads = [threading.Thread(target=hold, args=(a,)) for a in (a1, a2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]
    claims = net.store.all("SELECT * FROM claims WHERE function_id=3")
    assert len(claims) == 1


def test_q03_failed_claim_charges_no_turn(net):
    """A claim that cannot be acquired consumes no search budget (plan Q-03)."""
    proj = make_project(net, 5)
    alice = net.authenticate(net.register_agent("alice"))
    net.claim(alice, function="func_003")
    fn = net.store.one("SELECT * FROM functions WHERE id=3")
    Permuter(net)._permute(alice, proj, fn, State())
    assert net.store.one("SELECT turns_used FROM search_budgets WHERE function_id=3") is None


def test_q04_expired_claim_recovers_work(net):
    """A worker that died mid-lease stops hiding its function once the lease expires (plan Q-04)."""
    proj = make_project(net, 5)
    with net.store.tx() as db:
        db.execute("INSERT INTO agents(id, name, owner, token_hash) VALUES(1,'dead',NULL,'t1')")
        db.execute("INSERT INTO claims(function_id, agent_id, claimed_at, expires_at) VALUES(3, 1, 0, 1)")
    perm = Permuter(net)
    assert 3 in [f["id"] for f in perm.candidates(proj, limit=20)]
    fn = net.store.one("SELECT * FROM functions WHERE id=3")
    assert perm._hold(Agent(99, "permuter", None, True), fn)
    row = net.store.one("SELECT agent_id FROM claims WHERE function_id=3")
    assert row["agent_id"] == 99  # the dead claim was expunged, the new lease granted


def test_q05_partly_used_budget_persists_across_restart(net):
    """Part-used and exhausted budget state survives a restart coherently (plan Q-05)."""
    proj = make_project(net, 10)
    exhaust(net, range(1, 6))
    perm = Permuter(net)
    fn = net.store.one("SELECT * FROM functions WHERE id=10")
    assert perm._charge_turn(fn) == 1
    assert perm._charge_turn(fn) == 2
    fresh = Permuter(Network(net.store.conn.execute("PRAGMA database_list").fetchone()["file"]))
    row = fresh.net.store.one("SELECT turns_used FROM search_budgets WHERE function_id=10")
    assert row["turns_used"] == 2
    ids = [f["id"] for f in fresh.candidates(proj, limit=20)]
    assert 10 in ids and all(i not in ids for i in range(1, 6))


def test_q06_material_improvement_resets_budget_cosmetics_do_not(net):
    """A strictly better agent source grants a bounded fresh budget; equal scores and the permuter's
    own improvements do not (plan Q-06 / P1.2 steps 5-6)."""
    proj = make_project(net, 5)
    exhaust(net, range(1, 4))
    alice = net.authenticate(net.register_agent("alice"))
    fn = net.store.one("SELECT * FROM functions WHERE id=3")
    # cosmetic: same score, different text
    net._record(fn, alice, fn["best_score"], "/* same score */ int func_003(void) { return 3; }", 1)
    row = net.store.one("SELECT turns_used, reset_reason FROM search_budgets WHERE function_id=3")
    assert row["turns_used"] == MAX_TURNS and row["reset_reason"] is None
    # material: strictly better score from an agent
    net._record(fn, alice, fn["best_score"] - 1, "int func_003(void) { return 0; }", 1)
    row = net.store.one("SELECT turns_used, reset_reason FROM search_budgets WHERE function_id=3")
    assert row["turns_used"] == 0 and row["reset_reason"] == "better-source"
    # the permuter improving its own search must not reset its own budget
    perm = Permuter(net)
    perm._charge_turn(fn)
    perm._charge_turn(fn)
    net._record(fn, alice, fn["best_score"] - 2, "int func_003(void) { return 0; }", 1, via="permuter")
    row = net.store.one("SELECT turns_used FROM search_budgets WHERE function_id=3")
    assert row["turns_used"] == 2


def test_q06b_context_change_resets_budgets_for_the_unit(net):
    """A committed header change resets the budgets of the functions that compile against it, and
    only those (plan Q-06: context changes are material new input)."""
    proj = make_project(net, 5)
    exhaust(net, range(1, 5))
    with net.store.tx() as db:
        db.execute("UPDATE functions SET unit='unit_a' WHERE id IN (1, 2)")
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 2, 'int a;', NULL, 'm', 0, 'unit_a')")
        net._reset_budgets_for_context(db, 1, "unit_a", 2)
    for i in (1, 2):
        row = net.store.one("SELECT turns_used, reset_reason FROM search_budgets WHERE function_id=?", i)
        assert row["turns_used"] == 0 and row["reset_reason"] == "context-changed"
    row = net.store.one("SELECT turns_used FROM search_budgets WHERE function_id=3")
    assert row["turns_used"] == MAX_TURNS
