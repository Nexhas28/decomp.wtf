"""SQLite persistence. One file, one connection, one lock: simple and plenty fast for a
coordinator whose expensive work (compiling) happens outside the lock."""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, owner TEXT, token_hash TEXT UNIQUE NOT NULL,
  created_at REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT, description TEXT, arch TEXT,
  big_endian INTEGER NOT NULL DEFAULT 0, root TEXT NOT NULL, config TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS context_versions(
  project_id INTEGER, version INTEGER, header TEXT, agent_id INTEGER, message TEXT, created_at REAL,
  PRIMARY KEY(project_id, version));
CREATE TABLE IF NOT EXISTS functions(
  id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, name TEXT NOT NULL, size INTEGER, n_insns INTEGER,
  target BLOB, relocs TEXT, asm TEXT, seed TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  best_score INTEGER, best_source TEXT, best_agent_id INTEGER,
  matched_source TEXT, matched_agent_id INTEGER, matched_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0, abandons INTEGER NOT NULL DEFAULT 0,
  UNIQUE(project_id, name));
CREATE TABLE IF NOT EXISTS claims(
  function_id INTEGER PRIMARY KEY, agent_id INTEGER NOT NULL, claimed_at REAL, expires_at REAL);
CREATE TABLE IF NOT EXISTS attempts(
  id INTEGER PRIMARY KEY, function_id INTEGER, agent_id INTEGER, score INTEGER, compiled INTEGER,
  context_version INTEGER, source TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY, project_id INTEGER, function_id INTEGER, agent_id INTEGER, text TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, project_id INTEGER, agent_id INTEGER, kind TEXT, detail TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS idx_functions_status ON functions(project_id, status, n_insns);
CREATE INDEX IF NOT EXISTS idx_attempts_fn ON attempts(function_id);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(created_at);
CREATE TABLE IF NOT EXISTS registrations(ip_hash TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS symbols(
  project_id INTEGER NOT NULL, name TEXT NOT NULL, address INTEGER NOT NULL, size INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'data', preview TEXT, PRIMARY KEY(project_id, name));
CREATE INDEX IF NOT EXISTS idx_symbols_addr ON symbols(project_id, address);
CREATE TABLE IF NOT EXISTS twin_misses(
  function_id INTEGER NOT NULL, source_hash TEXT NOT NULL, project_id INTEGER, at REAL, why TEXT,
  PRIMARY KEY(function_id, source_hash));
CREATE TABLE IF NOT EXISTS search_budgets(
  function_id INTEGER PRIMARY KEY, epoch TEXT NOT NULL DEFAULT '', policy_version INTEGER NOT NULL DEFAULT 1,
  turns_used INTEGER NOT NULL DEFAULT 0, max_turns INTEGER NOT NULL DEFAULT 4,
  improvements INTEGER NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0,
  reset_reason TEXT, last_at REAL);
CREATE TABLE IF NOT EXISTS attempt_snapshots(
  attempt_id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, function_id INTEGER NOT NULL,
  function_revision INTEGER NOT NULL DEFAULT 1, context_version INTEGER NOT NULL,
  context_digest TEXT NOT NULL, target_digest TEXT NOT NULL, compiler_digest TEXT NOT NULL,
  scorer_version INTEGER NOT NULL DEFAULT 1, policy_version INTEGER NOT NULL DEFAULT 1,
  stale_reason TEXT);
CREATE TABLE IF NOT EXISTS verification_receipts(
  function_id INTEGER PRIMARY KEY, attempt_id INTEGER, snapshot_id TEXT NOT NULL,
  source_digest TEXT NOT NULL, candidate_object_digest TEXT, origin TEXT NOT NULL,
  object_match TEXT, function_link_match TEXT NOT NULL,
  unit_code_data_match TEXT NOT NULL DEFAULT 'unverified',
  whole_image_checksum TEXT NOT NULL DEFAULT 'not_run',
  acceptance_policy TEXT NOT NULL, verified_at_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidates(
  id INTEGER PRIMARY KEY, function_id INTEGER NOT NULL, origin TEXT NOT NULL,
  source TEXT NOT NULL, source_digest TEXT NOT NULL, parent_id INTEGER,
  evidence TEXT, score_version INTEGER, created_at REAL);
CREATE INDEX IF NOT EXISTS idx_candidates_fn ON candidates(function_id);
CREATE TABLE IF NOT EXISTS function_walls(
  function_id INTEGER NOT NULL, signature TEXT NOT NULL, reason TEXT, at REAL,
  PRIMARY KEY(function_id, signature));
CREATE TABLE IF NOT EXISTS merge_proposals(
  id INTEGER PRIMARY KEY, function_id INTEGER NOT NULL, continues_id INTEGER NOT NULL,
  at REAL, evidence TEXT);
CREATE TABLE IF NOT EXISTS reference_sources(
  entry_id TEXT NOT NULL, project_id INTEGER,
  repository TEXT NOT NULL, revision TEXT NOT NULL, source_location TEXT NOT NULL,
  toolchain TEXT NOT NULL, profile TEXT NOT NULL, target_digest TEXT NOT NULL,
  processor TEXT NOT NULL DEFAULT 'mips', mode TEXT NOT NULL DEFAULT '',
  provenance TEXT NOT NULL DEFAULT '', usage_restrictions TEXT NOT NULL DEFAULT '[]',
  source_available INTEGER NOT NULL DEFAULT 1, source_digest TEXT NOT NULL DEFAULT '',
  holdout INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
  PRIMARY KEY(project_id, entry_id));
CREATE INDEX IF NOT EXISTS idx_reference_sources_project ON reference_sources(project_id);
CREATE TABLE IF NOT EXISTS search_runs(
  function_id INTEGER PRIMARY KEY, snapshot_key TEXT NOT NULL, policy_epoch TEXT NOT NULL,
  checkpoint TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS function_identity(
  function_id INTEGER PRIMARY KEY, schema_version INTEGER NOT NULL DEFAULT 1,
  artifact_sha256 TEXT, module_id TEXT NOT NULL DEFAULT '', unit_id TEXT NOT NULL DEFAULT '',
  address_space TEXT NOT NULL DEFAULT '', entry_kind TEXT NOT NULL DEFAULT 'unknown', entry TEXT,
  section TEXT, recorded_by TEXT NOT NULL DEFAULT 'backfill');
CREATE TABLE IF NOT EXISTS evidence_records(
  evidence_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL DEFAULT 1, function_key TEXT NOT NULL,
  target_digest TEXT NOT NULL, provider_route TEXT NOT NULL, underlying_engine TEXT NOT NULL,
  engine_version TEXT NOT NULL, analysis_profile_digest TEXT NOT NULL, correlation_group TEXT NOT NULL,
  snapshot TEXT NOT NULL, status TEXT NOT NULL, facets TEXT NOT NULL DEFAULT '{}',
  observations TEXT NOT NULL DEFAULT '[]', inferences TEXT NOT NULL DEFAULT '[]',
  unknowns TEXT NOT NULL DEFAULT '[]', limitations TEXT NOT NULL DEFAULT '[]',
  untrusted_target_content INTEGER NOT NULL DEFAULT 1, original_provider_evidence_ref TEXT,
  classification TEXT, entry TEXT, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_evidence_fn ON evidence_records(function_key, snapshot);
CREATE TABLE IF NOT EXISTS evidence_payloads(
  payload_ref TEXT PRIMARY KEY, body TEXT NOT NULL, size INTEGER NOT NULL, created_at REAL NOT NULL);
"""

#: Serialised form of the Section 18.2 function identity (bump when its JSON shape changes).
IDENTITY_SCHEMA_VERSION = 1


def hex_entry(value: int | None) -> str | None:
    """Exact hexadecimal string for a cross-language address/offset (18.2): lower-case, 0x-prefixed,
    at least 8 digits, so JSON consumers never lose precision and equal entries compare equal."""
    return None if value is None else f"0x{int(value):08x}"

# Columns added after the first release; applied in place to existing databases.
MIGRATIONS = [
    ("agents", "trusted", "INTEGER NOT NULL DEFAULT 0"),      # operator-vetted (CLI) vs. self-registered
    ("agents", "origin", "TEXT"),                              # cli | web
    ("agents", "terms_accepted_at", "REAL"),                   # evidence of accepting the terms
    ("functions", "fingerprint", "TEXT"),                      # one-way match fingerprint (hash-only projects)
    ("attempts", "via", "TEXT"),                               # agent | permuter
    ("functions", "unit", "TEXT NOT NULL DEFAULT ''"),         # source file (translation unit) it came from
    ("context_versions", "unit", "TEXT NOT NULL DEFAULT ''"),  # '' = project-wide header, else per-unit header
    ("functions", "vram", "INTEGER"),                          # ROM projects: load address of the function
    ("functions", "rom_offset", "INTEGER"),                    # ROM projects: where its bytes are in the ROM
    ("functions", "meta", "TEXT"),                             # forks: symbol, demangled name, source file
    ("functions", "boost", "INTEGER NOT NULL DEFAULT 0"),      # claim order: >0 matching it matches that many twins
    ("functions", "revision", "INTEGER NOT NULL DEFAULT 1"),   # split/merge/target change = new revision (Section 18.2)
    ("projects", "matched_epoch", "INTEGER NOT NULL DEFAULT 0"),  # incremented on every acceptance (P1.4 epochs)
    ("functions", "best_verified", "INTEGER NOT NULL DEFAULT 1"),  # 0: legacy best scored before snapshots; revalidate before trusting
    ("agents", "bridge", "TEXT"),  # the bridge daemon's stable client id (same id = same identity across restarts)
    ("search_budgets", "lifetime_resets", "INTEGER NOT NULL DEFAULT 0"),  # better-source resets so far (capped, P1.2)
    ("search_budgets", "compiled_turns", "INTEGER NOT NULL DEFAULT 0"),   # turns that produced a compiled candidate
    ("search_budgets", "improved_turns", "INTEGER NOT NULL DEFAULT 0"),   # turns that improved the best score
    ("search_budgets", "infra_failures", "INTEGER NOT NULL DEFAULT 0"),   # turns lost to infrastructure failures
    ("functions", "best_triage", "TEXT"),                      # one-line verdict on the best attempt's diff (the brief opens with it)
]


class Store:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        for table, column, ddl in MIGRATIONS:
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_attempts_agent_time ON attempts(agent_id, created_at)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_functions_unit ON functions(project_id, unit, status)")
        # the progress the GUI asks for every second (totals, the map's "anything new?"), answered from this narrow
        # index instead of reading every function's assembly and sources: 31 ms -> about 2 for 36,000 functions
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_functions_progress ON functions(project_id, status, size, "
                          "attempts, best_score, matched_at)")
        # agents created before `origin` existed were all minted by the operator on the CLI
        self.conn.execute("UPDATE agents SET trusted = 1, origin = 'cli' WHERE origin IS NULL")
        # removing a project left its symbols behind before 0.10.3, and the next import (which may get the same id)
        # ran into them
        self.conn.execute("DELETE FROM symbols WHERE project_id NOT IN (SELECT id FROM projects)")
        # before 0.10.4 a twin's try left the function claimed by the autopilot for 30 minutes, and each expiry
        # counted as an agent giving up on it: thousands of functions were put last for nothing. Counted again,
        # once, from what agents actually did (releases, and their own claims that ran out)
        if self.conn.execute("SELECT 1 FROM settings WHERE key='abandons_recounted'").fetchone() is None:
            self.conn.executescript("""
                BEGIN;
                CREATE TEMP TABLE gave_up AS SELECT project_id, detail AS name, COUNT(*) AS n FROM events
                  WHERE kind='release' OR (kind='expired' AND COALESCE(agent_id, -1) NOT IN
                    (SELECT id FROM agents WHERE name IN ('autopilot', 'permuter')))
                  GROUP BY project_id, detail;
                CREATE INDEX temp.gave_up_name ON gave_up(project_id, name);
                UPDATE functions SET abandons = COALESCE((SELECT n FROM gave_up g WHERE g.project_id=functions.project_id
                                                          AND g.name=functions.name), 0) WHERE abandons > 0;
                DROP TABLE gave_up;
                INSERT INTO settings(key, value) VALUES('abandons_recounted', '1');
                COMMIT;""")
        self.heal_statuses(self.conn)
        self.backfill_identities(self.conn)
        self.lock = threading.RLock()

    # A status is a short word (open, matched, asm, nosource). On 16 October 2026 something writing straight to a
    # PS1 project's database put functions' assembly in it instead: they showed as "to do" but no strategy claim
    # ever picked them. A garbled status is put right on opening, and a new one is refused (with the culprit's
    # error) instead of stored.
    _BAD_STATUS = "(status = '' OR length(status) > 24 OR status GLOB '*[^a-z_]*')"

    @classmethod
    def heal_statuses(cls, db) -> int:
        """Functions whose status isn't a status word go back to matched (they have matched source) or open."""
        n = db.execute(f"UPDATE functions SET status = CASE WHEN matched_source IS NOT NULL THEN 'matched' "
                       f"ELSE 'open' END WHERE {cls._BAD_STATUS}").rowcount
        if n:
            logging.getLogger("decomp.wtf").warning("put right %d functions whose status was garbled", n)
        bad = cls._BAD_STATUS.replace("status", "NEW.status")
        for event in ("INSERT", "UPDATE OF status"):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS functions_status_{event.split()[0].lower()} BEFORE {event} "
                       f"ON functions WHEN {bad} BEGIN SELECT RAISE(ABORT, 'a function status must be a short word "
                       f"like open or matched'); END")
        return n

    @staticmethod
    def backfill_identities(db) -> None:
        """Idempotent Section 18.2 migration: drop identity rows whose function is gone (ids can be reused),
        then attach an identity to every function that has none: the paths that insert functions without
        one (forks, rescans, Game Boy/GBA imports, an older version writing after a downgrade). Only what
        the functions table already knows is used; the artifact digest stays NULL rather than invented."""
        db.execute("DELETE FROM function_identity WHERE function_id NOT IN (SELECT id FROM functions)")
        db.execute(
            "INSERT INTO function_identity(function_id, schema_version, artifact_sha256, module_id, unit_id, "
            "address_space, entry_kind, entry, section, recorded_by) "
            "SELECT f.id, ?, NULL, COALESCE(f.unit, ''), COALESCE(f.unit, ''), "
            "CASE WHEN f.vram IS NULL THEN '' ELSE 'vram:' || COALESCE(f.unit, '') END, "
            "CASE WHEN f.vram IS NULL THEN 'unknown' ELSE 'virtual_address' END, "
            "CASE WHEN f.vram IS NULL THEN NULL ELSE printf('0x%08x', f.vram) END, NULL, 'backfill' "
            "FROM functions f WHERE NOT EXISTS (SELECT 1 FROM function_identity i WHERE i.function_id = f.id)",
            (IDENTITY_SCHEMA_VERSION,))

    @staticmethod
    def record_identity(db, function_id: int, *, artifact_sha256: str | None, module_id: str, unit_id: str,
                        address_space: str, entry_kind: str, entry: str | None, section: str | None,
                        recorded_by: str = "import") -> None:
        """Attach (or refresh) a function's identity inside the caller's transaction. Name and revision are
        not copied here: they stay in `functions`, the one mutable truth for labels and revisions."""
        db.execute(
            "INSERT INTO function_identity(function_id, schema_version, artifact_sha256, module_id, unit_id, "
            "address_space, entry_kind, entry, section, recorded_by) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(function_id) DO UPDATE SET schema_version=excluded.schema_version, "
            "artifact_sha256=excluded.artifact_sha256, module_id=excluded.module_id, unit_id=excluded.unit_id, "
            "address_space=excluded.address_space, entry_kind=excluded.entry_kind, entry=excluded.entry, "
            "section=excluded.section, recorded_by=excluded.recorded_by",
            (function_id, IDENTITY_SCHEMA_VERSION, artifact_sha256, module_id, unit_id, address_space,
             entry_kind, entry, section, recorded_by))

    @staticmethod
    def record_rom_identities(db, project_id: int, artifact_sha256: str) -> None:
        """Identities for a freshly imported ROM project: entry is the load address, and the segment/bank
        (the function's unit) names the address space, since overlays and banks reuse addresses."""
        db.execute("DELETE FROM function_identity WHERE function_id IN (SELECT id FROM functions WHERE project_id=?)",
                   (project_id,))
        db.execute(
            "INSERT INTO function_identity(function_id, schema_version, artifact_sha256, module_id, unit_id, "
            "address_space, entry_kind, entry, section, recorded_by) "
            "SELECT id, ?, ?, unit, unit, 'vram:' || unit, "
            "CASE WHEN vram IS NULL THEN 'unknown' ELSE 'virtual_address' END, "
            "CASE WHEN vram IS NULL THEN NULL ELSE printf('0x%08x', vram) END, NULL, 'import' "
            "FROM functions WHERE project_id=?", (IDENTITY_SCHEMA_VERSION, artifact_sha256, project_id))

    def identity(self, function_id: int) -> dict | None:
        """The Section 18.2 function-identity record, as cross-language JSON-ready data."""
        row = self.one(
            "SELECT i.*, f.project_id, f.name, f.revision FROM function_identity i "
            "JOIN functions f ON f.id = i.function_id WHERE i.function_id = ?", function_id)
        if row is None:
            return None
        return {"schema_version": row["schema_version"], "project_id": row["project_id"],
                "artifact_sha256": row["artifact_sha256"], "module_id": row["module_id"],
                "unit_id": row["unit_id"], "address_space": row["address_space"],
                "entry_kind": row["entry_kind"], "entry": row["entry"], "section": row["section"],
                "function_revision": row["revision"], "display_name": row["name"]}

    @contextmanager
    def tx(self):
        """Serialized transaction. Never compile while holding this."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def one(self, sql: str, *args) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def all(self, sql: str, *args) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()
