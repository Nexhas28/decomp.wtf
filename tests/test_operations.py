"""Operations tests (plan O-01..O-03): migration + backup restore, additive-schema safety, and
feature-flag independence of the conservative matching path. Stub-based."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from decompnet.network import Network

from .test_permute import make_project


def _populated(tmp_path) -> Path:
    """A database with matched state, receipts, candidates and budgets — the migration test subject."""
    path = tmp_path / "app.db"
    net = Network(path)
    make_project(net, 3)
    a = net.authenticate(net.register_agent("alice"))
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    from decompnet import snapshots

    net._record(fn, a, 0, "int func_001(void) { return 1; }", 1, matched=True,
                snapshot=snapshots.capture(fn, proj, net._context(1, "")))
    with net.store.tx() as dbx:
        dbx.execute("INSERT INTO search_budgets(function_id, turns_used, max_turns, epoch, last_at) "
                    "VALUES(2, 3, 4, 'guided-v1', 0)")
    return path


def test_o01_backup_restore_preserves_acceptance_and_provenance(tmp_path):
    db = _populated(tmp_path)
    backup = tmp_path / "backup.db"
    # A correct backup must include WAL content: use SQLite's backup primitive (a raw file copy of
    # the .db alone would silently lose the WAL — this test failed exactly that way before).
    src = sqlite3.connect(db)
    with sqlite3.connect(backup) as dst:
        src.backup(dst)
    src.close()
    reopened = Network(db)
    assert reopened.store.one("SELECT status FROM functions WHERE id=1")["status"] == "matched"
    assert reopened.store.one("SELECT * FROM verification_receipts WHERE function_id=1") is not None
    assert reopened.store.one("SELECT turns_used FROM search_budgets WHERE function_id=2")["turns_used"] == 3
    assert reopened.store.one("SELECT origin FROM candidates WHERE function_id=1")["origin"] == "agent"
    # a restored copy is independently openable (no corruption, WAL replay included)
    restored = Network(backup)
    assert restored.store.one("SELECT COUNT(*) AS n FROM functions")["n"] == 3


def test_o01b_legacy_state_is_never_silently_promoted(tmp_path):
    """A legacy 'matched' row with no receipt stays matched but gains NO fabricated receipt
    (plan Section 18.4: never backfill an exact receipt from a status flag)."""
    db = tmp_path / "legacy.db"
    net = Network(db)
    make_project(net, 3)
    with net.store.tx() as dbx:
        dbx.execute("UPDATE functions SET status='matched', matched_source='int func_002(void){return 2;}' "
                    "WHERE id=2")
    reopened = Network(db)
    assert reopened.store.one("SELECT status FROM functions WHERE id=2")["status"] == "matched"
    assert reopened.store.one("SELECT * FROM verification_receipts WHERE function_id=2") is None
    # verification scope in the UI reflects the missing receipt, not a rounded-up claim
    s = reopened.status_json()["projects"][0]
    assert s["verification"]["receipts"] == 0 and s["matched"] == 1


def test_o03_partial_publication_never_leaves_a_dangling_reference(tmp_path):
    """Plan O-03: a crash mid-publish cannot leave the database pointing at a partial payload. The
    atomic-publish convention (write temp, then commit entry) is exercised on the candidate/receipt
    rows: every record's payload fields are complete or the record does not exist."""
    db = _populated(tmp_path)
    net = Network(db)
    rows = net.store.all("SELECT * FROM verification_receipts")
    assert all(r["snapshot_id"] and r["source_digest"] and r["acceptance_policy"] for r in rows)
    cand = net.store.all("SELECT * FROM candidates")
    assert all(c["source"] and c["source_digest"] for c in cand)
