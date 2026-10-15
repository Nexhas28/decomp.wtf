"""Versioned attempt snapshots and verification receipts (plan P1.3, Section 18.3/18.4).

No compiler needed: snapshots are content digests over database state.
"""

from __future__ import annotations

import pytest

from decompnet import snapshots
from decompnet.network import Network

from .test_permute import make_project  # shared synthetic-project fixture


@pytest.fixture
def net(tmp_path):
    n = Network(tmp_path / "snap.db")
    make_project(n, 3)
    return n


@pytest.fixture
def alice(net):
    return net.authenticate(net.register_agent("alice"))


def ctx(net, fn):
    return net._context(fn["project_id"], fn["unit"])


def test_capture_digests_are_content_addressed(net):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    snap = snapshots.capture(fn, proj, ctx(net, fn))
    assert snap["schema"] == snapshots.SNAPSHOT_SCHEMA
    assert snap["function_revision"] == 1
    assert snap["context_version"] == ctx(net, fn)["version"]
    # changing any material input changes its digest
    assert snapshots.target_digest(fn) != snapshots.target_digest({**fn, "rom_offset": 12345})
    assert snapshots.context_digest(ctx(net, fn)) != snapshots.context_digest({**ctx(net, fn), "header": "int x;\n"})
    assert snapshots.compiler_digest(proj) != snapshots.compiler_digest(
        {**proj, "config": '{"compiler": {"toolchain": "gcc-2.8.1-psx", "flags": ["-O0"]}}'})


def test_check_current_detects_each_dependency_class(net):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    snap = snapshots.capture(fn, proj, ctx(net, fn))
    ok, why = snapshots.check_current(net, proj, fn, snap)
    assert ok and not why

    # function revision bump (split / operator reset)
    with net.store.tx() as db:
        db.execute("UPDATE functions SET revision=revision+1 WHERE id=1")
    ok, why = snapshots.check_current(net, proj, fn, snap)
    assert not ok and "split or reset" in why
    with net.store.tx() as db:
        db.execute("UPDATE functions SET revision=revision-1 WHERE id=1")

    # target bytes changed
    with net.store.tx() as db:
        db.execute("UPDATE functions SET target=randomblob(16) WHERE id=1")
    ok, why = snapshots.check_current(net, proj, fn, snap)
    assert not ok and "target changed" in why
    with net.store.tx() as db:
        db.execute("UPDATE functions SET target=NULL WHERE id=1")

    # context changed (new version)
    with net.store.tx() as db:
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(1,2,'int func_xxx(void); int extra;',NULL,'m',0,'')")
    ok, why = snapshots.check_current(net, proj, fn, snap)
    assert not ok and "context changed" in why


def test_attempt_records_its_snapshot(net, alice, monkeypatch):
    """A recorded attempt carries the dependency snapshot it was compiled under (P1.3)."""
    fn = net.store.one("SELECT * FROM functions WHERE id=1")

    class FakeResult:
        ok = False
        log = "fake compile error"

    monkeypatch.setattr(net, "_compile", lambda proj, header, source: FakeResult())
    out = net.attempt(alice, fn["name"], "int func_001(void) { return 1; }")
    assert "COMPILE ERROR" in out["text"]
    row = net.store.one(
        "SELECT s.* FROM attempt_snapshots s JOIN attempts a ON a.id=s.attempt_id "
        "WHERE a.function_id=?", fn["id"])
    assert row is not None
    assert row["context_version"] == ctx(net, fn)["version"]
    assert row["target_digest"] == snapshots.target_digest(fn)


def test_match_writes_receipt_and_bumps_matched_epoch(net, alice):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    before = net.store.one("SELECT matched_epoch FROM projects WHERE id=1")["matched_epoch"]
    net._record(fn, alice, 0, "int func_001(void) { return 1; }", 1, matched=True,
                snapshot=snapshots.capture(fn, proj, ctx(net, fn)))
    r = net.store.one("SELECT * FROM verification_receipts WHERE function_id=1")
    assert r is not None
    assert r["function_link_match"] == "exact"
    assert r["acceptance_policy"] == f"rom-function-policy-v{snapshots.POLICY_VERSION}"
    assert r["snapshot_id"] == snapshots.snapshot_id(snapshots.capture(fn, proj, ctx(net, fn)))
    assert net.store.one("SELECT matched_epoch FROM projects WHERE id=1")["matched_epoch"] == before + 1


def test_reset_function_bumps_revision_and_clears_receipts(net, alice):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    net._record(fn, alice, 0, "int func_001(void) { return 1; }", 1, matched=True,
                snapshot=snapshots.capture(fn, proj, ctx(net, fn)))
    assert net.store.one("SELECT revision FROM functions WHERE id=1")["revision"] == 1
    net.reset_function("fixture", "func_001")
    assert net.store.one("SELECT revision FROM functions WHERE id=1")["revision"] == 2
    assert net.store.one("SELECT * FROM verification_receipts WHERE function_id=1") is None
    assert net.store.one("SELECT * FROM attempt_snapshots WHERE function_id=1") is None


def test_snapshots_and_receipts_survive_restart(net, alice):
    """Additive schema + records persist across a coordinator restart (plan O-01's migration leg)."""
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    net._record(fn, alice, 0, "int func_001(void) { return 1; }", 1, matched=True,
                snapshot=snapshots.capture(fn, proj, ctx(net, fn)))
    path = net.store.conn.execute("PRAGMA database_list").fetchone()["file"]
    reopened = Network(path)
    assert reopened.store.one("SELECT * FROM verification_receipts WHERE function_id=1") is not None
    assert reopened.store.one("SELECT matched_epoch FROM projects WHERE id=1")["matched_epoch"] == 1
    assert len(reopened.store.all("SELECT * FROM attempt_snapshots")) == 1
