"""Candidate provenance (plan P4.6): distinct sources are retained as origin-tagged alternatives,
never silently overwritten by the latest draft; alternatives are only current-best after rescoring
under the current snapshot. Stub-based: no compiler needed."""

from __future__ import annotations

import pytest

from decompnet import snapshots
from decompnet.network import Network

from .test_permute import make_project


@pytest.fixture
def net(tmp_path):
    n = Network(tmp_path / "cand.db")
    make_project(n, 3)
    return n


@pytest.fixture
def alice(net):
    return net.authenticate(net.register_agent("alice"))


def ctx(net, fn):
    return net._context(fn["project_id"], fn["unit"])


def test_every_distinct_source_is_retained_with_origin(net, alice):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    snap = snapshots.capture(fn, proj, ctx(net, fn))
    net._record(fn, alice, 10, "int func_001(void) { return 1; }", 1, via="agent", snapshot=snap)
    net._record(fn, alice, 8, "int func_001(void) { return 2; }", 1, via="permuter", snapshot=snap)
    rows = net.store.all("SELECT origin, source_digest FROM candidates WHERE function_id=1 ORDER BY id")
    assert len(rows) == 2
    assert [r["origin"] for r in rows] == ["agent", "permuter"]
    assert rows[0]["source_digest"] != rows[1]["source_digest"]


def test_duplicate_sources_are_deduplicated(net, alice):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    snap = snapshots.capture(fn, proj, ctx(net, fn))
    src = "int func_001(void) { return 1; }"
    for _ in range(3):
        net._record(fn, alice, 10, src, 1, via="agent", snapshot=snap)
    assert net.store.one("SELECT COUNT(*) AS n FROM candidates WHERE function_id=1")["n"] == 1


def test_candidate_retention_is_bounded(net, alice):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    snap = snapshots.capture(fn, proj, ctx(net, fn))
    for i in range(55):
        net._record(fn, alice, 100 - i, f"int func_001(void) {{ return {i}; }}", 1, via="agent", snapshot=snap)
    assert net.store.one("SELECT COUNT(*) AS n FROM candidates WHERE function_id=1")["n"] == 50


def test_alternatives_reports_snapshot_freshness(net, alice):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    snap = snapshots.capture(fn, proj, ctx(net, fn), symbols_digest=snapshots.symbol_table_digest(net.store, proj["id"]))
    net._record(fn, alice, 10, "int func_001(void) { return 1; }", 1, via="agent", snapshot=snap)
    out = net.alternatives("func_001")
    assert "agent (current-snapshot)" in out
    # a header change makes the retained candidate historical
    with net.store.tx() as db:
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(1,2,'int func_xxx(void); int newer;',NULL,'m',0,'')")
    out = net.alternatives("func_001")
    assert "agent (historical)" in out


def test_match_records_the_matched_source_as_candidate(net, alice):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    net._record(fn, alice, 0, "int func_001(void) { return 1; }", 1, matched=True,
                snapshot=snapshots.capture(fn, proj, ctx(net, fn)))
    rows = net.store.all("SELECT origin FROM candidates WHERE function_id=1")
    assert [r["origin"] for r in rows] == ["agent"]


def test_reset_function_clears_candidates(net, alice):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    net._record(fn, alice, 10, "int func_001(void) { return 1; }", 1,
                snapshot=snapshots.capture(fn, proj, ctx(net, fn)))
    net.reset_function("fixture", "func_001")
    assert net.store.one("SELECT COUNT(*) AS n FROM candidates WHERE function_id=1")["n"] == 0
