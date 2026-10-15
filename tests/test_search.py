"""PR-18 (P5.4-P5.6): guided mismatch-aware search — routing, the diverse portfolio, dedup, the
versioned search-policy spec and the permuter wiring (plan tests P-04..P-06, P-08).

Pure Python: no compiler or toolchain is involved. Fake evaluators stand in for the compile/score
path; the network-side tests exercise only the persisted budget/epoch wiring.
"""

from __future__ import annotations

import pytest

from decompnet.app.permute import MAX_TURNS, Permuter
from decompnet.network import Network
from decompnet.permuter import EXPLORATORY, Mutation, Span, digest
from decompnet.search import (
    SEARCH_POLICY,
    SearchPolicy,
    SearchPolicyError,
    SearchResumeError,
    EvalOutcome,
    checkpoint_run,
    new_run,
    parse_policy,
    resume_run,
    route,
    step,
)

SRC = """s32 collide(s32 a, s32 b) {
    s32 x;
    s32 y;
    x = a + b;
    y = a * b;
    if (a < b) {
        y = x;
    }
    return y;
}
"""


def make_evaluator(scores=None, object_key=None, compile_errors=frozenset()):
    """A deterministic fake compile/score: score by content lookup or hash, object identity optional."""
    calls = []

    def evaluate(source):
        calls.append(source)
        if source in compile_errors:
            return EvalOutcome(score=None, kind="compile_error")
        score = scores(source) if callable(scores) else (1 if scores is None else scores)
        return EvalOutcome(score=score, kind="scored", counts={},
                           object_digest=object_key(source) if object_key else digest(source))

    return evaluate, calls


def fake_mutation(source: str, parent: str, operator: str = "test_op") -> Mutation:
    return Mutation(operator=operator, category=EXPLORATORY, parent_digest=digest(parent),
                    child_digest=digest(source), spans=(Span.single(0, 0, 1),), text=source,
                    note="test")


def queued_propose(entries):
    """A propose override that serves `(child_source, operator)` entries in order, parented on the best."""
    it = iter(entries)

    def propose(run):
        try:
            src, op = next(it)
        except StopIteration:
            return run.portfolio.best, None
        return run.portfolio.best, fake_mutation(src, run.portfolio.best.source, op)

    return propose


def step_all(run, eval_fn, propose=None, limit=50):
    results = []
    for _ in range(limit):
        res = step(run, eval_fn, propose)
        if res is None:
            break
        results.append(res)
    return results


# ------------------------------------------------------------------ P-04

def test_p04_same_seed_source_and_policy_replay_the_same_sequence():
    """Same (seed, source, policy, snapshot, evaluator) replays the same mutation sequence (P-04)."""
    def run_once():
        r = new_run(SEARCH_POLICY, "replay-seed", "snap-1", SRC, 7, counts={"register": 5},
                    coverage="complete")
        return step_all(r, make_evaluator(3)[0])

    first, second = run_once(), run_once()
    assert len(first) > 10
    assert [(o.step, o.kind, o.operator, o.category, o.child_digest, o.score) for o in first] == \
           [(o.step, o.kind, o.operator, o.category, o.child_digest, o.score) for o in second]


def test_p04b_checkpoint_resume_continues_identically():
    """A resumed run continues exactly where the checkpoint left off (P5.5 step 7)."""
    def drive(r, n):
        return step_all(r, make_evaluator(3)[0], limit=n)

    a = new_run(SEARCH_POLICY, "resume-seed", "snap-1", SRC, 7, counts={"register": 5},
                coverage="complete")
    drive(a, 10)
    cp = checkpoint_run(a)
    cont_a = drive(a, 10)

    b = new_run(SEARCH_POLICY, "resume-seed", "snap-1", SRC, 7, counts={"register": 5},
                coverage="complete")
    drive(b, 10)
    b2 = resume_run(SEARCH_POLICY, "snap-1", cp)
    cont_b = drive(b2, 10)
    assert [(o.operator, o.kind, o.child_digest) for o in cont_a] == \
           [(o.operator, o.kind, o.child_digest) for o in cont_b]
    assert checkpoint_run(a) == checkpoint_run(b2)


def test_p04c_resume_rejects_stale_snapshot_or_policy():
    r = new_run(SEARCH_POLICY, "s", "snap-1", SRC, 7)
    cp = checkpoint_run(r)
    with pytest.raises(SearchResumeError):
        resume_run(SEARCH_POLICY, "snap-2", cp)  # snapshot changed
    with pytest.raises(SearchResumeError):
        resume_run(SearchPolicy(id="guided", version=2), "snap-1", cp)  # policy changed


def test_p04d_different_seeds_produce_different_sequences():
    def sequence(seed):
        r = new_run(SEARCH_POLICY, seed, "snap-1", SRC, 7)
        return [(o.operator, o.child_digest, o.kind) for o in step_all(r, make_evaluator(3)[0])]

    assert sequence("seed-one") != sequence("seed-two")


# ------------------------------------------------------------------ P-05

def test_p05_duplicate_objects_reuse_evaluation_and_keep_both_sources():
    """Two distinct sources with one object digest: one evaluation result, both sources retained."""
    a = "int f(void) {\n    int x;\n    x = 1;\n    return x;\n}\n"
    b = "int f(void) {\n\n    int x;\n    x = 1;\n    return x;\n}\n"  # same object, different shape
    policy = SearchPolicy(id="t", version=1, beam=4, plateau=0.0, base_weights=dict(SEARCH_POLICY.base_weights))
    scores = {a: 3, b: 3}
    r = new_run(policy, "dup-obj", "snap-1", SRC, 5, counts={}, coverage="complete")
    results = step_all(r, make_evaluator(scores.get, object_key=lambda s: "obj-X")[0],
                       queued_propose([(a, "op_a"), (b, "op_b")]))
    kinds = [o.kind for o in results]
    assert kinds[0] == "improving"  # a beats the seed
    assert kinds[1] == "duplicate_object"
    assert r.stats["duplicate_object"] == 1 and r.stats["scored"] == 2
    assert len(r.evals.objects) == 1  # one object identity
    sources = {m.source for m in r.portfolio.members}
    assert a in sources and b in sources  # both sources retained for future rewrites (P5.6)


def test_p05b_duplicate_sources_reuse_the_evaluation():
    """The same source proposed twice is compiled once (source-digest dedup)."""
    a = "int f(void) {\n    int x;\n    x = 2;\n    return x;\n}\n"
    eval_fn, calls = make_evaluator({a: 2}.get)
    r = new_run(SEARCH_POLICY, "dup-src", "snap-1", SRC, 5)
    results = step_all(r, eval_fn, queued_propose([(a, "op_a"), (a, "op_a")]))
    assert results[1].kind == "duplicate_source"
    assert r.stats["duplicate_source"] == 1
    assert len(calls) == 1  # evaluated once


def test_p05c_metrics_distinguish_each_failure_class():
    """duplicate-source / compile-error / non-improving are distinct counters (P5.6)."""
    bad = "int f(void) {\n    int x;\n    x = ;\n}\n"
    good = "int f(void) {\n    int x;\n    x = 7;\n    return x;\n}\n"
    policy = SearchPolicy(id="t", version=1, beam=4, plateau=0.0, base_weights=dict(SEARCH_POLICY.base_weights))
    r = new_run(policy, "metrics", "snap-1", SRC, 1)  # seed already better than anything below
    results = step_all(r, make_evaluator({good: 4}.get, compile_errors={bad})[0],
                       queued_propose([(bad, "op_bad"), (good, "op_good"), (bad, "op_bad")]))
    kinds = [o.kind for o in results[:3]]
    assert kinds == ["compile_error", "non_improving", "duplicate_source"]
    assert r.stats["compile_error"] == 1
    assert r.stats["non_improving"] == 1
    assert r.stats["duplicate_source"] == 1


# ------------------------------------------------------------------ P-06

def test_p06_beam_stays_bounded_and_deterministic():
    """The beam never exceeds the policy bound and a plateau run is fully deterministic (P5.5/P5.6)."""
    policy = SearchPolicy(id="t", version=1, beam=4, plateau=0.02, max_steps=30,
                          base_weights=dict(SEARCH_POLICY.base_weights))

    def drive(seed):
        r = new_run(policy, seed, "snap-1", SRC, 5)
        return r, step_all(r, make_evaluator(5)[0])  # every candidate ties the best: plateau

    r, results = drive("plateau-1")
    r2, again = drive("plateau-1")
    assert len(results) == policy.max_steps and step(r, make_evaluator(5)[0]) is None  # exhausted
    assert len(r.portfolio.members) <= policy.beam
    assert [(o.operator, o.kind, o.child_digest) for o in results] == \
           [(o.operator, o.kind, o.child_digest) for o in again]
    assert checkpoint_run(r) == checkpoint_run(r2)  # identical state


def test_p06b_beam_one_matches_the_single_trajectory_contract():
    """beam=1 reproduces the legacy current/best trajectory: one member, downhill acceptance."""
    policy = SearchPolicy(id="t", version=1, beam=1, plateau=0.0, base_weights=dict(SEARCH_POLICY.base_weights))
    r = new_run(policy, "single", "snap-1", SRC, 5)
    step_all(r, make_evaluator(5)[0], limit=20)
    assert len(r.portfolio.members) == 1
    assert r.portfolio.best.score == 5


def test_p06c_source_growth_and_step_bounds_hold():
    policy = SearchPolicy(id="t", version=1, beam=2, max_source_chars=64, max_steps=3,
                          base_weights=dict(SEARCH_POLICY.base_weights))
    huge = "int f(void) { " + "x = 1; " * 30 + "}\n"
    eval_fn, calls = make_evaluator(2)
    r = new_run(policy, "bounds", "snap-1", SRC, 5)
    results = step_all(r, eval_fn, queued_propose([(huge, "op_big")]), limit=10)
    assert results[0].kind == "size_rejected"
    assert len(calls) == 0  # rejected before evaluation
    assert r.steps == policy.max_steps  # the step budget is enforced


def test_p06d_matched_candidates_stop_the_run():
    solved = "int f(void) {\n    return 0;\n}\n"
    r = new_run(SEARCH_POLICY, "match", "snap-1", SRC, 5)
    results = step_all(r, make_evaluator(0)[0], queued_propose([(solved, "op_solve")]))
    assert results[0].kind == "matched"
    assert r.matched and r.portfolio.best.score == 0
    assert step(r, make_evaluator(0)[0]) is None  # no work after a match


def test_p06e_propose_failures_are_recorded():
    r = new_run(SEARCH_POLICY, "nop", "snap-1", SRC, 5)
    res = step(r, make_evaluator(3)[0], propose=lambda run: (run.portfolio.best, None))
    assert res.kind == "propose_failure"
    assert r.stats["propose_failure"] == 1


# -------------------------------------------------------------- P5.4 routing

def test_routing_maps_register_mismatches_to_decl_and_temporary_families():
    d = route({"register": 7, "operand": 1}, "complete", SEARCH_POLICY)
    assert d.families == ("reorder_decls", "temp_introduce", "temp_eliminate")
    assert all(d.weights[op] == SEARCH_POLICY.base_weights[op] * 4 for op in d.families)
    assert "register" in d.reason and d.coverage == "complete"
    assert d.weights["retype_local"] >= SEARCH_POLICY.base_weights["retype_local"]  # floor, never excluded


def test_routing_maps_operand_mismatches_to_type_and_comparison_families():
    d = route({"operand": 5}, "complete", SEARCH_POLICY)
    assert set(d.families) == {"retype_local", "cast_add", "cast_remove", "comparison_flip", "cond_bool_form"}
    assert d.weights["cast_add"] == SEARCH_POLICY.base_weights["cast_add"] * 4


def test_routing_maps_reorder_to_dependency_safe_families():
    d = route({"reorder": 3}, "complete", SEARCH_POLICY)
    assert "swap_statements" in d.families and d.weights["swap_statements"] == 8  # base 2 * 4


def test_routing_maps_insertion_deletion_to_control_flow_families():
    d = route({"insertion": 2, "deletion": 1}, "complete", SEARCH_POLICY)
    assert set(d.families) == {"return_ternary", "branch_swap", "loop_bound_explore", "while_for", "inc_style"}


def test_routing_never_boosts_permutation_without_structured_evidence():
    """Literal/data or encoding-only differences route to the conservative mixed portfolio (P5.4)."""
    d = route({}, "complete", SEARCH_POLICY)
    assert d.families == ()
    assert d.weights == SEARCH_POLICY.base_weights
    assert "literal/data" in d.reason
    d2 = route({"register": 0, "operand": 0}, "prefix_limited", SEARCH_POLICY)
    assert d2.families == () and d2.coverage == "prefix_limited"


def test_routing_is_recorded_deterministic_and_validated():
    first = route({"register": 2}, "complete", SEARCH_POLICY)
    second = route({"register": 2}, "complete", SEARCH_POLICY)
    assert first == second
    assert route.__name__ and first.to_dict()["counts"] == {"register": 2}
    from decompnet.search.routing import RoutingDecision
    assert RoutingDecision.from_dict(first.to_dict()) == first
    with pytest.raises(ValueError):
        route({"literal": 1}, "complete", SEARCH_POLICY)  # unknown mismatch key


def test_run_records_the_routing_decision():
    r = new_run(SEARCH_POLICY, "r", "snap-1", SRC, 7, counts={"register": 4}, coverage="complete")
    assert r.routing.families == ("reorder_decls", "temp_introduce", "temp_eliminate")
    summary = r.summary()
    assert summary["routing"]["reason"]
    assert summary["seed"] == "r" and summary["snapshot_key"] == "snap-1"


# -------------------------------------------------------------- P-08 spec

def test_policy_spec_round_trips():
    assert SearchPolicy.from_dict(SEARCH_POLICY.to_dict()) == SEARCH_POLICY
    assert parse_policy(SEARCH_POLICY.to_dict()) is SEARCH_POLICY
    assert SEARCH_POLICY.epoch == "guided-v1"


def test_policy_spec_rejects_unknown_versions_and_invalid_fields():
    with pytest.raises(SearchPolicyError):
        parse_policy({"id": "guided", "version": 99})  # a future policy this process doesn't know
    with pytest.raises(SearchPolicyError):
        parse_policy({"id": "other", "version": 1})
    with pytest.raises(SearchPolicyError):
        SearchPolicy.from_dict({"id": "x", "version": 1, "beam": 0})
    with pytest.raises(SearchPolicyError):
        SearchPolicy.from_dict({"id": "x", "version": 1, "plateau": 1.0})
    with pytest.raises(SearchPolicyError):
        SearchPolicy.from_dict({"id": "x", "version": 1, "base_weights": {"not_an_operator": 2}})


def test_policy_spec_carries_seed_identity_requirements():
    """The spec + run identities are everything a distributed worker needs to replay (P-08)."""
    d = SEARCH_POLICY.to_dict()
    assert {"id", "version", "beam", "plateau", "max_steps", "routing", "base_weights"} <= set(d)
    r = new_run(SEARCH_POLICY, "job-seed-42", "snap-1", SRC, 7)
    assert r.seed == "job-seed-42" and r.slot_seeds and all(s.startswith("job-seed-42:slot") for s in r.slot_seeds)


# ------------------------------------------------------- permuter wiring (app)

def make_project(net: Network):
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'fixture', 'fixture', 'search fixture', 'mips', 0, '/tmp/fixture', '{}')")
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, 'int func_001(void);', NULL, 'fixture', 0, '')")
        db.execute(
            "INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, seed, status, "
            "best_score, best_source, best_agent_id, attempts, abandons) VALUES(1,1,'func_001',400,100,"
            "NULL,'[]','',NULL,'open',3,'/* best */\\nint func_001(void) { return 3; }',NULL,0,0)")
    return net.store.one("SELECT * FROM projects WHERE id=1")


@pytest.fixture
def net(tmp_path):
    return Network(tmp_path / "search.db")


def test_charge_turn_stamps_the_search_policy_epoch(net):
    proj = make_project(net)
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    perm = Permuter(net)
    assert perm._charge_turn(fn) == 1
    assert perm._charge_turn(fn) == 2
    row = net.store.one("SELECT * FROM search_budgets WHERE function_id=1")
    assert row["turns_used"] == 2
    assert row["epoch"] == SEARCH_POLICY.epoch  # epoch = search-policy version (P5.8)
    assert row["policy_version"] == SEARCH_POLICY.version


def test_stale_policy_budget_resets_and_is_eligible_again(net):
    """A budget stamped with an older policy version neither hides the function nor carries over."""
    proj = make_project(net)
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    perm = Permuter(net)
    with net.store.tx() as db:
        db.execute("INSERT INTO search_budgets(function_id, turns_used, max_turns, last_at, epoch, "
                   "policy_version) VALUES(1,?,?,0,'guided-v0',0)", (MAX_TURNS, MAX_TURNS))
    assert [f["id"] for f in perm.candidates(proj)] == [1]  # old policy version: eligible again
    perm._policy_reset(fn)
    row = net.store.one("SELECT * FROM search_budgets WHERE function_id=1")
    assert row["turns_used"] == 0 and row["reset_reason"] == "policy-changed"
    assert row["policy_version"] == SEARCH_POLICY.version and row["epoch"] == SEARCH_POLICY.epoch
    assert perm._charge_turn(fn) == 1  # the reset budget charges real work


def test_current_policy_exhausted_budget_still_hides_work(net):
    """Q-01 parity: an exhausted budget under the CURRENT policy version keeps the function hidden."""
    proj = make_project(net)
    perm = Permuter(net)
    with net.store.tx() as db:
        db.execute("INSERT INTO search_budgets(function_id, turns_used, max_turns, last_at, epoch, "
                   "policy_version) VALUES(1,?,?,0,?,?)",
                   (MAX_TURNS, MAX_TURNS, SEARCH_POLICY.epoch, SEARCH_POLICY.version))
    assert [f["id"] for f in perm.candidates(proj)] == []
