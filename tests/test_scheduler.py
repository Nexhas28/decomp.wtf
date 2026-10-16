"""Adaptive scheduling (plan W-01, W-04, W-05): explainable priority policy, global resource
budget, truthful verification scope in status. Stub-based, no compiler needed."""

from __future__ import annotations

import time

import pytest

from decompnet import scheduler
from decompnet.network import Network
from decompnet.scheduler import BUDGET, ResourceBudget, choose, explain, priority

from .test_permute import make_project


@pytest.fixture
def net(tmp_path):
    n = Network(tmp_path / "sched.db")
    make_project(n, 6)
    with n.store.tx() as db:
        # make functions distinct along every policy dimension
        db.execute("UPDATE functions SET boost=2, best_score=300, attempts=1 WHERE id=1")   # twin + near
        db.execute("UPDATE functions SET boost=0, best_score=50, attempts=0 WHERE id=2")    # very near, cheap
        db.execute("UPDATE functions SET boost=0, best_score=9900, attempts=40 WHERE id=3")  # far + costly
        db.execute("UPDATE functions SET boost=0, best_score=NULL, attempts=0 WHERE id=4")  # unknown
        db.execute("UPDATE functions SET unit='u1', boost=0, best_score=100 WHERE id IN (5,6)")
        db.execute("UPDATE functions SET status='matched' WHERE id=6")
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(1,2,'int u1;',NULL,'m',0,'u1')")
        db.execute("INSERT INTO attempts(function_id, agent_id, score, compiled, context_version, source, created_at) "
                   "VALUES(1,1,300,1,1,'x',0),(1,1,300,1,1,'y',0),(3,1,9900,1,1,'z',0)")
    return n


def alice(net):
    return net.authenticate(net.register_agent("alice"))


def test_w01_priority_is_explainable_and_uses_every_dimension(net):
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    fn1 = net.store.one("SELECT * FROM functions WHERE id=1")
    e = explain(net, fn1, proj)
    assert e["version"] == "priority-v1"
    assert set(e["components"]) == {"progress", "twin", "unit", "fairness", "cost", "no_progress"}
    assert e["components"]["twin"]["value"] == 0.5  # boost 2 -> 2/4
    assert e["components"]["progress"]["value"] > 0
    # the cheap near-match beats the untried unknown, which beats the costly far function
    fn2 = net.store.one("SELECT * FROM functions WHERE id=2")
    fn3 = net.store.one("SELECT * FROM functions WHERE id=3")
    fn4 = net.store.one("SELECT * FROM functions WHERE id=4")
    assert priority(net, fn2, proj) > priority(net, fn4, proj) > priority(net, fn3, proj)


def test_w01b_priority_claim_strategy_selects_and_explains(net):
    a = alice(net)
    brief = net.claim(a, strategy="priority")
    name = brief.splitlines()[0].split("/")[-1]
    # the boosted twin (id 1) or the cheap near-match (id 2) — never the costly far one (id 3) first
    assert name in ("func_001", "func_002")
    net.release(a, name, "")


def test_w01c_choose_is_deterministic_and_bounded(net):
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    rows = net.store.all("SELECT * FROM functions WHERE project_id=1 AND status='open'")
    first = [f["id"] for f in choose(net, proj, list(rows), 2)]
    second = [f["id"] for f in choose(net, proj, list(rows), 2)]
    assert first == second and len(first) == 2


def test_w01d_waiting_time_raises_priority(net):
    """W-01 aging: the same function's priority and fairness component grow as its waiting time grows."""
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    with net.store.tx() as db:
        db.execute("INSERT INTO attempts(function_id, agent_id, score, compiled, context_version, source, created_at) "
                   "VALUES(4,1,NULL,0,1,'w',?)", (time.time(),))
    priorities, fairness = [], []
    for hours in (1, 10, 72):
        with net.store.tx() as db:
            db.execute("UPDATE attempts SET created_at=? WHERE function_id=4", (time.time() - hours * 3600,))
        e = explain(net, net.store.one("SELECT * FROM functions WHERE id=4"), proj)
        priorities.append(e["priority"])
        fairness.append(e["components"]["fairness"]["value"])
    assert fairness[0] < fairness[1] < fairness[2]
    assert priorities[0] < priorities[1] < priorities[2]


def test_w01e_long_waiting_function_is_eventually_picked(net):
    """W-01 aging: a dominant near-match keeps winning while it stalls, but a long-waiting function
    is picked within a bounded number of picks."""
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    with net.store.tx() as db:
        db.execute("UPDATE functions SET boost=4, best_score=0, attempts=0 WHERE id=2")  # dominant near-match
        db.execute("UPDATE functions SET boost=0, best_score=0, attempts=0 WHERE id=4")  # long-waiting
        db.execute("INSERT INTO attempts(function_id, agent_id, score, compiled, context_version, source, created_at) "
                   "VALUES(4,1,0,1,1,'w',?)", (time.time() - 72 * 3600,))
    picks = []
    for _ in range(60):
        rows = [net.store.one("SELECT * FROM functions WHERE id=?", i) for i in (2, 4)]
        chosen = choose(net, proj, rows, 1)[0]
        if chosen["id"] == 4:
            break
        picks.append(chosen["id"])
        with net.store.tx() as db:  # the dominant function stalls: each pick is a non-improving attempt
            db.execute("INSERT INTO attempts(function_id, agent_id, score, compiled, context_version, source, "
                       "created_at) VALUES(?,1,1000,1,1,'p',?)", (chosen["id"], time.time()))
            db.execute("UPDATE functions SET attempts=attempts+1 WHERE id=?", (chosen["id"],))
    else:
        pytest.fail("the long-waiting function was never picked")
    assert picks and len(picks) <= 40


def test_w04_resource_budget_prevents_oversubscription():
    b = ResourceBudget({"compiler": 2, "external": 1})
    assert b.acquire("external") and not b.acquire("external")
    b.release("external")
    assert b.acquire("external")
    b.release("external")
    assert b.acquire("compiler") and b.acquire("compiler") and not b.acquire("compiler")
    with pytest.raises(KeyError):
        b.acquire("nope")
    with b.acquire_context("external"):
        assert not b.acquire("external")
        assert b.pressure()["used"]["external"] == 1
    assert b.pressure()["used"]["external"] == 0  # released on exit


def test_w04_slot_waits_times_out_and_reports_pressure():
    b = ResourceBudget({"compiler": 1})
    assert b.limits["compiler"] == 1
    with b.slot("compiler"):
        assert b.pressure()["used"]["compiler"] == 1
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            with b.slot("compiler", timeout=0.1):
                pass
        assert time.monotonic() - started >= 0.09
        with pytest.raises(TimeoutError):
            with b.slot("compiler", timeout=0):
                pass
        assert b.pressure()["waiting"]["compiler"] == 0  # a timed-out wait does not linger
    assert b.pressure()["used"]["compiler"] == 0
    with pytest.raises(KeyError):
        with b.slot("nope"):
            pass
    with b.slot("compiler", timeout=0):  # free again: no wait needed
        pass


def test_w04_default_compiler_limit_matches_the_old_slot_count():
    import os

    assert ResourceBudget().limits["compiler"] == max(2, (os.cpu_count() or 4) - 1)


def test_w04_real_compiler_calls_respect_the_budget(monkeypatch):
    """W-04 / P11.6: concurrent compile_source calls go through the global 'compiler' pool, so with a
    limit of 1 they never overlap (and a waiting caller shows up in pressure())."""
    import subprocess
    import threading

    from decompnet import compiler

    monkeypatch.setattr(scheduler, "BUDGET", ResourceBudget({"compiler": 1}))
    lock = threading.Lock()
    state = {"now": 0, "peak": 0, "calls": 0, "waiting_seen": 0}
    both_started = threading.Barrier(2)

    def fake_exec(cfg, work, argv, timeout, cwd=None, *, snapshot=None, **stage):
        with lock:
            state["now"] += 1
            state["calls"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.15)  # long enough for the other caller to reach the pool
        with lock:
            state["waiting_seen"] = max(state["waiting_seen"], scheduler.BUDGET.pressure()["waiting"]["compiler"])
            state["now"] -= 1
        (work / "func.o").write_bytes(b"obj")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(compiler, "_exec", fake_exec)
    cfg = compiler.CompilerConfig(cmd=["cc", "{src}"], sandbox="none", cache=False)
    results = []

    def go(i):
        both_started.wait(timeout=5)
        results.append(compiler.compile_source(cfg, "", f"int f{i}(void) {{ return {i}; }}"))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 2 and all(r.ok for r in results)
    assert state["calls"] == 2
    assert state["peak"] == 1  # never overlapped
    assert state["waiting_seen"] == 1  # the second caller was queued behind the first
    assert scheduler.BUDGET.pressure()["used"]["compiler"] == 0


def test_w05_status_shows_verification_scope_and_origins(net):
    a = alice(net)
    fn = net.store.one("SELECT * FROM functions WHERE id=2")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    from decompnet import snapshots

    net._record(fn, a, 0, "int func_002(void) { return 2; }", 1, matched=True,
                snapshot=snapshots.capture(fn, proj, net._context(1, "")))
    s = net.status_json()["projects"][0]
    assert s["verification"]["receipts"] == 1 and s["verification"]["matched_epoch"] == 1
    assert s["origins"] == {"agent": 1}
    # conservative percent: never rounds a near-match up to a claim it didn't earn
    assert s["matched"] == 2  # the fixture's pre-matched func_006 plus the freshly matched func_002
    assert s["matched_bytes"] <= s["bytes"]


# ------------------------------------------------------------------- W-02: new evidence

def test_w02_new_evidence_regrants_a_bounded_retry_with_explicit_reason(net):
    """W-02: an exhausted function gets a bounded retry when new evidence arrives, and the reset
    reason names that evidence explicitly — never a silent or unbounded budget grant."""
    from decompnet.app.permute import Permuter

    alice = net.authenticate(net.register_agent("alice"))
    proj = net.store.one("SELECT * FROM projects WHERE id=1")

    # evidence kind 1: a committed header change over a walled function (toolchain wall -> cooldown)
    net.claim(alice, function="func_002")
    out = net.release(alice, "func_002", "jump-table: Illegal operands (lui)")
    assert "Wall recorded" in out
    walled = net.store.one("SELECT turns_used, max_turns, cooldown_until, reset_reason "
                           "FROM search_budgets WHERE function_id=2")
    assert walled["reset_reason"].startswith("wall:") and walled["turns_used"] == walled["max_turns"]
    assert walled["cooldown_until"] > 0
    # strategy claims skip the wall; a by-name claim still works
    other = net.claim_row(alice, "fixture", strategy="easiest")
    assert other is not None and other["name"] != "func_002"
    net.release(alice, other["name"], "")  # hand the strategy claim back before the by-name claim
    with net.store.tx() as db:
        db.execute("UPDATE functions SET unit='u2' WHERE id=2")
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, "
                   "created_at, unit) VALUES(1, 3, 'int u2;', NULL, 'evidence', 0, 'u2')")
        net._reset_budgets_for_context(db, 1, "u2", 3)
    row = net.store.one("SELECT turns_used, max_turns, cooldown_until, reset_reason "
                        "FROM search_budgets WHERE function_id=2")
    assert row["reset_reason"] == "context-changed"  # the reason names the evidence
    assert row["turns_used"] == 0 and row["cooldown_until"] == 0 and row["max_turns"] == 4  # bounded
    assert net.store.one("SELECT COUNT(*) AS n FROM function_walls WHERE function_id=2")["n"] == 1
    assert net.claim_row(alice, "fixture", "func_002")["name"] == "func_002"  # claimable again

    # evidence kind 2: a strictly better agent source over an exhausted (non-wall) budget
    fn5 = net.store.one("SELECT * FROM functions WHERE id=5")  # a near-miss (id 3 is deliberately far)
    with net.store.tx() as db:
        db.execute("INSERT INTO search_budgets(function_id, turns_used, max_turns, last_at) "
                   "VALUES(5, 4, 4, 0)")
    net._record(fn5, alice, int(fn5["best_score"] * 0.9), "int func_005(void) { return 0; }", 1)
    row = net.store.one("SELECT turns_used, max_turns, reset_reason FROM search_budgets WHERE function_id=5")
    assert row["turns_used"] == 0 and row["reset_reason"] == "better-source" and row["max_turns"] == 4
    ids = [f["id"] for f in Permuter(net).candidates(proj, limit=50)]
    assert 5 in ids  # the bounded retry is actually available to the permuter


# ------------------------------------------------------------------- W-03: bounded briefs

def test_w03_briefs_stay_bounded_under_repeated_long_dossiers(net):
    """W-03: a function handed off repeatedly with long dossiers still gets a bounded brief —
    note ingress is capped per note and per query, so the brief cannot grow with handoff count."""
    alice = net.authenticate(net.register_agent("alice"))
    fn = net.store.one("SELECT * FROM functions WHERE id=2")
    big = "x" * 9000  # far beyond the per-note cap
    for i in range(8):
        net.add_note(alice, "fixture", f"{big}-{i}", function="func_002")
    for i in range(5):
        net.add_note(alice, "fixture", f"{big}-p{i}")
    # the cap is enforced at ingress: no unbounded text ever reaches a brief
    assert net.store.one("SELECT LENGTH(text) AS n FROM notes WHERE function_id=2 LIMIT 1")["n"] == 4000
    brief = net._brief(fn, alice, "CLAIMED")
    assert len(brief) <= 65_000  # bounded: 8 function notes + 5 project notes, each capped at 4000
    assert "xxxx" in brief  # the bounded dossier content is still present


# ------------------------------------------------- W-06: benchmark experiment identity

def test_w06_benchmark_manifest_identity_separates_runs(tmp_path):
    """W-06: the harness manifest carries a unique experiment identity per run and retains the
    settings (stub/provider config) and cost summary — a changed configuration is a new, separate
    experiment, never a silent overwrite of the previous run's artifacts."""
    import json
    import os
    import subprocess
    import sys

    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    harness = repo / "benchmarks" / "harness" / "replay.py"
    net = Network(tmp_path / "replay.db")
    make_project(net, 3)
    net.store.conn.close()  # hand the database over to the harness subprocesses
    attempts = tmp_path / "attempts.jsonl"
    attempts.write_text("".join(
        json.dumps({"function": f"func_{i:03d}", "source": f"int func_{i:03d}(void) {{ return {i}; }}"}) + "\n"
        for i in range(1, 4)), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(repo / "src")}
    base = [sys.executable, str(harness), "--db", str(tmp_path / "replay.db"), "--project", "fixture",
            "--attempts", str(attempts), "--out", str(tmp_path / "results")]
    for stub in ("compile-error", "score-5"):  # different provider/config per run
        subprocess.run(base + ["--stub", stub], env=env, check=True, capture_output=True, text=True,
                       timeout=120)

    run_dirs = sorted((tmp_path / "results").iterdir())
    assert len(run_dirs) == 2  # two experiments, two separate result directories
    manifests = []
    for d in run_dirs:
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        assert set(m) == {"schema_version", "run_id", "started_utc", "kind", "stub", "db", "project",
                          "attempts_in"}
        assert m["schema_version"] == 1 and m["kind"] == "fixed-input-replay"
        assert m["project"] == "fixture" and m["attempts_in"] == 3
        manifests.append(m)
        s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
        assert s["total_attempts"] == 3 and s["compiles"] == 3 and s["errors"] == 0
        assert (d / "stage-timings.jsonl").exists()
    assert len({m["run_id"] for m in manifests}) == 2  # experiment identity separates runs
    assert {m["stub"] for m in manifests} == {"compile-error", "score-5"}  # settings retained per run
