"""P12.3 downgrade safety (plan Section 24): a database migrated by this version must stay usable by
the previous version, and the migration itself must be additive by construction.

The fork's rule is that schema change is ONLY `ALTER TABLE ... ADD COLUMN` with a default (store.py
MIGRATIONS) or `CREATE TABLE IF NOT EXISTS` (store.py SCHEMA) — never a drop, rename or type change.
This test enforces that rule mechanically and then proves the practical consequence: a writer that
knows only the pre-migration columns still works against a fully migrated database, and a reader that
knows only the old columns sees what it always saw.
"""

from __future__ import annotations

import re

import pytest

from decompnet import store as store_mod
from decompnet.network import Network

from .test_permute import make_project


def test_every_migration_is_additive():
    """No migration may drop, rename or retype: the previous version must be able to keep writing."""
    forbidden = re.compile(r"\b(DROP|DELETE|RENAME|TRUNCATE)\b", re.I)
    assert store_mod.MIGRATIONS, "expected the migration list to be non-empty"
    for table, column, ddl in store_mod.MIGRATIONS:
        assert not forbidden.search(ddl), f"{table}.{column} migration is destructive: {ddl!r}"
        assert "NOT NULL" not in ddl or "DEFAULT" in ddl, (
            f"{table}.{column} is NOT NULL without a DEFAULT: an older writer's INSERT would fail")
    # the schema itself only ever creates
    for stmt in [s.strip() for s in store_mod.SCHEMA.split(";") if s.strip()]:
        upper = stmt.upper()
        if upper.startswith("CREATE"):
            continue
        assert upper.startswith(("INSERT", "UPDATE")), f"unexpected non-create SCHEMA statement: {stmt[:60]!r}"


def test_columns_added_by_migration_all_carry_defaults_or_are_nullable(tmp_path):
    """The concrete failure mode: an older version's INSERT that omits a newer column must succeed."""
    net = Network(tmp_path / "downgrade.db")
    make_project(net, 2)
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    # the pre-migration column set (everything except the columns added later)
    added = {column for _, column, _ in store_mod.MIGRATIONS if column in fn.keys()}
    columns = [c for c in fn.keys() if c not in added]
    with net.store.tx() as db:
        # an "old version" INSERT: only the columns it knows, targeted at the migrated table
        db.execute("INSERT INTO functions(project_id, name, status, unit) VALUES(1, 'old_writer_fn', 'open', '')")
        old = db.execute(f"SELECT {', '.join(columns)} FROM functions WHERE name='old_writer_fn'").fetchone()
        migrated = db.execute("SELECT * FROM functions WHERE name='old_writer_fn'").fetchone()
    assert old["name"] == "old_writer_fn"
    # every migrated NOT NULL column got its default rather than blocking the insert
    info = {r[1]: r for r in net.store.conn.execute("PRAGMA table_info(functions)")}
    for column in added:
        if info[column][3]:  # notnull
            assert migrated[column] is not None, f"NOT NULL column {column} left empty by an old insert"


def test_new_tables_do_not_disturb_an_old_reader(tmp_path):
    """New tables are invisible to a reader that never asks for them, and their absence is tolerated
    by the code paths that guard on them (the pre-PR-31 shape)."""
    net = Network(tmp_path / "downgrade2.db")
    make_project(net, 2)
    # an old reader's query, run against the migrated database
    rows = net.store.all("SELECT id, name, status, best_score FROM functions WHERE project_id=1 ORDER BY id")
    assert [r["name"] for r in rows][:2] == ["func_001", "func_002"]
    # the new tables exist but are empty, so an old code path sees no walls/proposals
    assert net.store.all("SELECT * FROM function_walls") == []
    assert net.store.all("SELECT * FROM merge_proposals") == []


def test_migration_is_idempotent_across_reopens(tmp_path):
    """Reopening the database re-runs the migration check: it must be a no-op, not an error."""
    path = tmp_path / "downgrade3.db"
    first = Network(path)
    make_project(first, 2)
    cols_before = {r[1] for r in first.store.conn.execute("PRAGMA table_info(functions)")}
    second = Network(path)  # a second process opening the same file
    cols_after = {r[1] for r in second.store.conn.execute("PRAGMA table_info(functions)")}
    assert cols_before == cols_after
    assert second.store.one("SELECT COUNT(*) AS n FROM functions")["n"] == 2
