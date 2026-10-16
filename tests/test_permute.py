"""Queue, lease and budget correctness for the background permuter (plan Q-01..Q-06).

These tests run without any compiler: they exercise the store/claim/budget logic directly.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from decompnet.app.permute import CHECKPOINT_LIMIT, MAX_TURNS, Permuter, State, TurnStats
from decompnet.network import Agent, Network
from decompnet.search import checkpoint_run, new_run
from decompnet.search.policy import SEARCH_POLICY, SINGLE_TRAJECTORY_POLICY, active_policy
from decompnet.toolchains import ToolchainError


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


@pytest.fixture(autouse=True)
def guided_search(monkeypatch):
    """The guided beam search is opt-in (plan P5 exit gate); the budget rows these tests build carry the
    guided policy's version, so they run with it switched on. Tests of the default unset the variable."""
    monkeypatch.setenv("DECOMPNET_GUIDED_SEARCH", "1")


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


def test_default_policy_is_single_trajectory_unless_guided_search_is_enabled(net, monkeypatch):
    """The guided beam search is opt-in: without DECOMPNET_GUIDED_SEARCH=1 the permuter charges and
    stamps budgets under the single-trajectory policy (plan P5 exit gate)."""
    make_project(net, 3)
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    monkeypatch.delenv("DECOMPNET_GUIDED_SEARCH", raising=False)
    assert active_policy() is SINGLE_TRAJECTORY_POLICY
    Permuter(net)._charge_turn(fn)
    row = net.store.one("SELECT epoch, policy_version FROM search_budgets WHERE function_id=1")
    assert row["epoch"] == SINGLE_TRAJECTORY_POLICY.epoch
    assert row["policy_version"] == SINGLE_TRAJECTORY_POLICY.version
    monkeypatch.setenv("DECOMPNET_GUIDED_SEARCH", "1")
    assert active_policy() is SEARCH_POLICY


def _reopen(net) -> Permuter:
    """A fresh Permuter over the same database file, as after a restart."""
    return Permuter(Network(net.store.conn.execute("PRAGMA database_list").fetchone()["file"]))


def test_checkpoint_survives_restart_and_incompatible_snapshot_is_rejected(net, monkeypatch):
    """A search-run checkpoint is persisted: a new Permuter resumes what a previous one saved, while a
    different snapshot key rejects it (and deletes the row) so a fresh run starts instead (plan P1.2 item 8)."""
    proj = make_project(net, 3)
    fn = net.store.one("SELECT * FROM functions WHERE id=2")
    policy = active_policy()
    first = Permuter(net)
    cp = checkpoint_run(new_run(policy, "saved-seed", "key-a", fn["best_source"], fn["best_score"]))
    first._save_checkpoint(fn["id"], cp)
    row = net.store.one("SELECT snapshot_key, policy_epoch FROM search_runs WHERE function_id=2")
    assert (row["snapshot_key"], row["policy_epoch"]) == ("key-a", policy.epoch)

    second = _reopen(net)
    assert second.checkpoints == {}
    monkeypatch.setattr(second, "_probe", lambda *a: pytest.fail("a compatible checkpoint must not start a fresh run"))
    run = second._resume_or_start(proj, fn, None, policy, "new-seed", "key-a")
    assert run.seed == "saved-seed" and run.snapshot_key == "key-a"
    assert net.store.one("SELECT 1 FROM search_runs WHERE function_id=2") is not None

    third = _reopen(net)
    monkeypatch.setattr(third, "_probe", lambda *a: (None, "unknown", None))
    run = third._resume_or_start(proj, fn, None, policy, "new-seed", "key-b")
    assert run.seed == "new-seed" and run.snapshot_key == "key-b"
    assert net.store.one("SELECT 1 FROM search_runs WHERE function_id=2") is None


def test_checkpoint_rows_are_bounded_and_dropped_with_the_checkpoint(net):
    """The search_runs table keeps at most CHECKPOINT_LIMIT rows (oldest updated_at first out), and
    dropping a checkpoint deletes its row."""
    make_project(net, 3)
    perm = Permuter(net)
    for fn_id in range(1, CHECKPOINT_LIMIT + 4):
        perm._save_checkpoint(fn_id, {"snapshot_key": "k"})
    ids = {r["function_id"] for r in net.store.all("SELECT function_id FROM search_runs")}
    assert ids == set(range(4, CHECKPOINT_LIMIT + 4))
    perm._drop_checkpoint(CHECKPOINT_LIMIT + 3)
    assert net.store.one("SELECT 1 FROM search_runs WHERE function_id=?", CHECKPOINT_LIMIT + 3) is None
    assert CHECKPOINT_LIMIT + 3 not in perm.checkpoints


def test_p12_toolchain_failure_refunds_the_turn(net, monkeypatch):
    """A turn that dies because the toolchain is missing consumes no search budget and counts as an
    infrastructure failure (plan P1.2); the claim is still released."""
    proj = make_project(net, 3)
    alice = net.authenticate(net.register_agent("alice"))
    fn = net.store.one("SELECT * FROM functions WHERE id=2")
    monkeypatch.setattr(net, "fork_slug", lambda slug: None, raising=False)

    def no_toolchain(*args, **kwargs):
        raise ToolchainError("no compiler installed")

    monkeypatch.setattr(net, "_compile", no_toolchain)
    with pytest.raises(ToolchainError):
        Permuter(net)._permute(alice, proj, fn, State())
    row = net.store.one("SELECT turns_used, compiled_turns, improved_turns, infra_failures FROM search_budgets "
                        "WHERE function_id=2")
    assert (row["turns_used"], row["compiled_turns"], row["improved_turns"], row["infra_failures"]) == (0, 0, 0, 1)
    assert net.store.all("SELECT * FROM claims WHERE function_id=2") == []


def test_p12_evaluate_failure_without_a_compile_is_refunded(net, monkeypatch):
    """A turn with an infrastructure failure and nothing compiled is refunded (never below zero); a plain
    compile error is not an infrastructure failure and keeps its charge."""
    proj = make_project(net, 3)
    alice = net.authenticate(net.register_agent("alice"))
    fn = net.store.one("SELECT * FROM functions WHERE id=2")
    perm = Permuter(net)
    stats = TurnStats()
    run = SimpleNamespace(portfolio=SimpleNamespace(best=SimpleNamespace(score=2)))
    monkeypatch.setattr(net, "_compile", lambda proj, header, source, fn=None: SimpleNamespace(ok=False, obj=b""))
    evaluate = perm._make_evaluator(alice, proj, fn, None, run, stats)
    assert evaluate("int func_002(void) { return 0; }").kind == "compile_error"
    assert not stats.compiled and stats.infra == 0
    perm._charge_turn(fn)
    perm._finish_turn(fn, stats)  # nothing compiled, nothing failed: the turn stands
    assert net.store.one("SELECT turns_used FROM search_budgets WHERE function_id=2")["turns_used"] == 1
    stats.infra += 1
    perm._finish_turn(fn, stats)
    row = net.store.one("SELECT turns_used, infra_failures FROM search_budgets WHERE function_id=2")
    assert (row["turns_used"], row["infra_failures"]) == (0, 1)
    perm._finish_turn(fn, stats)  # the refund never goes below zero
    assert net.store.one("SELECT turns_used FROM search_budgets WHERE function_id=2")["turns_used"] == 0


def test_p12_missing_toolchain_compile_result_is_infrastructure_and_refunded(net, monkeypatch):
    """A missing toolchain comes back as a failed CompileResult, not an exception: it counts as an
    infrastructure failure and the turn is refunded, while an ordinary compile error is neither."""
    make_project(net, 3)
    alice = net.authenticate(net.register_agent("alice"))
    fn = net.store.one("SELECT * FROM functions WHERE id=2")
    perm = Permuter(net)
    run = SimpleNamespace(portfolio=SimpleNamespace(best=SimpleNamespace(score=2)))
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    logs = iter(["compiler not found: mwccpsp.exe", "error: expected ';'",
                 "ToolchainError: gcc is gone", "toolchain not installed: run decompnet toolchain install x"])
    monkeypatch.setattr(net, "_compile",
                        lambda proj, header, source, fn=None: SimpleNamespace(ok=False, obj=None, log=next(logs)))
    expected = [(1, "evaluate_failure"), (0, "compile_error"), (1, "evaluate_failure"), (1, "evaluate_failure")]
    for infra, kind in expected:
        stats = TurnStats()
        out = perm._make_evaluator(alice, proj, fn, None, run, stats)("int func_002(void) { return 0; }")
        assert (stats.infra, out.kind, stats.compiled) == (infra, kind, False)
    stats = TurnStats(infra=1)
    perm._charge_turn(fn)
    perm._finish_turn(fn, stats)
    row = net.store.one("SELECT turns_used, infra_failures FROM search_budgets WHERE function_id=2")
    assert (row["turns_used"], row["infra_failures"]) == (0, 1)


def _routed_checkpoint(fn, counts: dict, coverage: str) -> dict:
    return checkpoint_run(new_run(active_policy(), "seed", "key", fn["best_source"], fn["best_score"],
                                  counts=counts, coverage=coverage))


def test_p54_call_reference_and_literal_mismatches_leave_the_queue_with_a_reason(net):
    """A function whose persisted routing decision says call/global-reference ('prototype/context first') or
    literal/data with complete-but-empty structured evidence needs context work, not mutation: it is skipped
    by candidates() and its budget row says 'needs-context'. Register mismatches and unknown evidence stay."""
    proj = make_project(net, 5)
    perm = Permuter(net)
    fns = {i: net.store.one("SELECT * FROM functions WHERE id=?", i) for i in range(1, 6)}
    perm._save_checkpoint(1, _routed_checkpoint(fns[1], {"operand": 5, "call_reference": 5}, "complete"))
    perm._save_checkpoint(2, _routed_checkpoint(fns[2], {}, "complete"))
    perm._save_checkpoint(3, _routed_checkpoint(fns[3], {"register": 3}, "complete"))
    perm._save_checkpoint(4, _routed_checkpoint(fns[4], {}, "unknown"))
    assert [f["id"] for f in perm.candidates(proj, limit=20)] == [3, 4, 5]
    for i in (1, 2):
        row = net.store.one("SELECT reset_reason, turns_used FROM search_budgets WHERE function_id=?", i)
        assert row["reset_reason"] == "needs-context" and row["turns_used"] == 0
    assert net.store.one("SELECT 1 FROM search_budgets WHERE function_id IN (3, 4, 5)") is None
    # a restart keeps it, and a material reset (new context) lifts it
    fresh = _reopen(net)
    assert [f["id"] for f in fresh.candidates(proj, limit=20)] == [3, 4, 5]
    with net.store.tx() as db:
        db.execute("UPDATE search_budgets SET reset_reason='context-changed' WHERE function_id=1")
    assert [f["id"] for f in fresh.candidates(proj, limit=20)] == [1, 3, 4, 5]


def test_p12_evaluate_failure_is_counted_and_compiled_turns_are_not_refunded(net, monkeypatch):
    """An object the evaluator cannot read counts as an infrastructure failure, but a turn in which
    something compiled keeps its charge and is counted in compiled_turns."""
    proj = make_project(net, 3)
    alice = net.authenticate(net.register_agent("alice"))
    fn = net.store.one("SELECT * FROM functions WHERE id=2")
    perm = Permuter(net)
    stats = TurnStats()
    run = SimpleNamespace(portfolio=SimpleNamespace(best=SimpleNamespace(score=2)))
    monkeypatch.setattr(net, "_compile", lambda proj, header, source, fn=None: SimpleNamespace(ok=True, obj=b""))
    monkeypatch.setattr(net, "_evaluate", lambda fn, proj, obj: (None, None, []))
    assert perm._make_evaluator(alice, proj, fn, None, run, stats)("int func_002(void) { return 0; }").kind \
        == "evaluate_failure"
    assert stats.compiled and stats.infra == 1
    perm._charge_turn(fn)
    perm._finish_turn(fn, stats)
    row = net.store.one("SELECT turns_used, compiled_turns, improved_turns, infra_failures FROM search_budgets "
                        "WHERE function_id=2")
    assert (row["turns_used"], row["compiled_turns"], row["improved_turns"], row["infra_failures"]) == (1, 1, 0, 0)
