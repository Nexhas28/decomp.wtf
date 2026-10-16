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
