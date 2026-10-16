"""Plan Section 18 data contracts: function identity (18.2), durable evidence records (18.5) and the
validated configuration schema with its effective-configuration diagnostic (18.9)."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import tomllib
from pathlib import Path

import pytest

from decompnet import cli
from decompnet.analysis.contracts import SCOPE_LIMITATION, EvidenceRecord, FunctionEvidence
from decompnet.analysis.evidence_store import EvidenceStore, record_from_evidence
from decompnet.network import Network, NetworkError, config_from
from decompnet.project import (ConfigError, ProjectRejected, effective_config, import_project, load_config,
                               validate_config)
from decompnet.store import SCHEMA, Store

from .test_analysis import FakeProvider, make_broker, register
from .test_permute import make_project

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "projects" / "demo"
N64 = ROOT / "projects" / "demo-n64"  # ships a prebuilt big-endian MIPS relocatable object: no compiler needed
HEX = re.compile(r"^0x[0-9a-f]{8,}$")


# ------------------------------------------------------------------------------------ 18.2 identity

@pytest.fixture
def n64(tmp_path, monkeypatch):
    from decompnet import seeds

    monkeypatch.setattr(seeds, "available", lambda: False)  # no m2c drafting: identity is all we test
    proj = tmp_path / "demo-n64"
    shutil.copytree(N64, proj)
    return proj


def test_object_import_records_section_relative_identity(tmp_path, n64):
    net = Network(tmp_path / "id.db")
    pid = import_project(net.store, n64, log=lambda *_: None)
    obj = (n64 / "target" / "game.o").read_bytes()
    rows = net.store.all("SELECT id, name, revision FROM functions WHERE project_id=?", pid)
    assert rows
    for row in rows:
        ident = net.store.identity(row["id"])
        assert ident["schema_version"] == 1 and ident["project_id"] == pid
        assert ident["artifact_sha256"] == hashlib.sha256(obj).hexdigest()
        assert ident["unit_id"] == "game" and ident["module_id"] == "main"
        assert ident["address_space"] == "object:game"
        # a relocatable object has no virtual address: section identity plus section offset
        assert ident["entry_kind"] == "section_offset"
        assert ident["section"].startswith(".text")
        assert HEX.match(ident["entry"])
        assert ident["display_name"] == row["name"] and ident["function_revision"] == row["revision"]
        json.dumps(ident)  # cross-language JSON, no ints that could lose precision


def test_identity_follows_renames_and_revisions_without_a_second_copy(tmp_path, n64):
    net = Network(tmp_path / "rename.db")
    import_project(net.store, n64, log=lambda *_: None)
    fid = net.store.one("SELECT id FROM functions LIMIT 1")["id"]
    before = net.store.identity(fid)
    with net.store.tx() as db:
        db.execute("UPDATE functions SET name='renamed_label', revision=revision+1 WHERE id=?", (fid,))
    after = net.store.identity(fid)
    assert after["display_name"] == "renamed_label"
    assert after["function_revision"] == before["function_revision"] + 1
    assert {k: v for k, v in after.items() if k not in ("display_name", "function_revision")} == \
        {k: v for k, v in before.items() if k not in ("display_name", "function_revision")}


def test_reimport_keeps_a_matched_function_identity(tmp_path, n64):
    net = Network(tmp_path / "reimport.db")
    pid = import_project(net.store, n64, log=lambda *_: None)
    fid = net.store.one("SELECT id FROM functions WHERE project_id=? LIMIT 1", pid)["id"]
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched' WHERE id=?", (fid,))
        db.execute("UPDATE function_identity SET artifact_sha256='old-digest' WHERE function_id=?", (fid,))
    import_project(net.store, n64, log=lambda *_: None)
    assert net.store.identity(fid)["artifact_sha256"] == "old-digest"  # matched under the old target
    others = net.store.all("SELECT function_id, artifact_sha256 FROM function_identity WHERE function_id != ?", fid)
    assert others and all(r["artifact_sha256"] != "old-digest" for r in others)


def test_rom_identity_puts_the_segment_in_the_address_space(tmp_path):
    net = Network(tmp_path / "rom.db")
    make_project(net, 1)
    with net.store.tx() as db:
        db.execute("INSERT INTO functions(project_id, name, unit, vram) VALUES(1, 'ovl_fn', 'overlay2', ?)",
                   (0x80001234,))
        db.execute("INSERT INTO functions(project_id, name, unit, vram) VALUES(1, 'main_fn', 'main', ?)",
                   (0x80001234,))  # same address in another segment: a different function
        Store.record_rom_identities(db, 1, "a" * 64)
    ovl = net.store.one("SELECT id FROM functions WHERE name='ovl_fn'")["id"]
    main = net.store.one("SELECT id FROM functions WHERE name='main_fn'")["id"]
    a, b = net.store.identity(ovl), net.store.identity(main)
    assert a["entry"] == b["entry"] == "0x80001234" and a["entry_kind"] == "virtual_address"
    assert (a["address_space"], a["module_id"]) == ("vram:overlay2", "overlay2")
    assert b["address_space"] == "vram:main"
    assert a["artifact_sha256"] == "a" * 64


def _pre_identity_database(path: Path) -> None:
    """A database as the previous version left it: everything except the Section 18 tables."""
    old_schema = re.sub(r"CREATE TABLE IF NOT EXISTS (function_identity|evidence_records|evidence_payloads)\(.*?\);",
                        "", SCHEMA, flags=re.S)
    old_schema = old_schema.replace(
        "CREATE INDEX IF NOT EXISTS idx_evidence_fn ON evidence_records(function_key, snapshot);", "")
    assert "function_identity" not in old_schema and "evidence_records" not in old_schema
    conn = sqlite3.connect(path)
    conn.executescript(old_schema)
    conn.close()


def test_identity_migration_backfills_an_old_database_and_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    _pre_identity_database(path)
    net = Network(path)  # the upgrade: creates the table
    make_project(net, 3)
    with net.store.tx() as db:
        db.execute("UPDATE functions SET unit='seg', vram=? WHERE id=2", (0x80000010,))
    Network(path)  # reopen: backfill the functions the fixture inserted without identity
    snapshot = lambda: [tuple(r) for r in sqlite3.connect(path).execute(
        "SELECT * FROM function_identity ORDER BY function_id")]
    first = snapshot()
    assert len(first) == 3
    ident = Network(path).store.identity(2)
    assert (ident["entry_kind"], ident["entry"], ident["address_space"]) == ("virtual_address", "0x80000010", "vram:seg")
    assert ident["artifact_sha256"] is None  # never invented by the backfill
    assert Network(path).store.identity(1)["entry_kind"] == "unknown"
    Network(path)
    assert snapshot() == first  # reopening is a no-op


def test_identity_survives_a_downgrade_round_trip(tmp_path):
    """New version writes identities; the previous version (which does not know the table) keeps reading and
    writing functions; the new version reopens and fills only the gaps, dropping identities of deleted rows."""
    path = tmp_path / "round.db"
    net = Network(path)
    make_project(net, 3)
    Network(path)
    # the previous version: plain sqlite, only the columns and tables it knows
    old = sqlite3.connect(path)
    old.execute("INSERT INTO functions(project_id, name, status, unit) VALUES(1, 'old_writer_fn', 'open', '')")
    old.execute("DELETE FROM functions WHERE id=3")
    old.commit()
    assert [r[0] for r in old.execute("SELECT name FROM functions WHERE project_id=1 ORDER BY id")] == \
        ["func_001", "func_002", "old_writer_fn"]
    old.close()
    upgraded = Network(path)
    ids = {r["function_id"] for r in upgraded.store.all("SELECT function_id FROM function_identity")}
    new_id = upgraded.store.one("SELECT id FROM functions WHERE name='old_writer_fn'")["id"]
    assert ids == {1, 2, new_id}
    assert upgraded.store.identity(new_id)["display_name"] == "old_writer_fn"


# ------------------------------------------------------------------------------------ 18.5 evidence

def _evidence():
    return FunctionEvidence(
        function_key="game::f", provider="fake", engine="ghidra-12.1.4", correlation_group="", snapshot="s1",
        status="ok", coverage={"pseudocode": "ok", "calls": "partial", "types": "unknown"},
        views={"pseudocode": {"text": "int f(void) { return 1; }", "truncated": False},
               "calls": {"items": [{"name": "g"}], "truncated": True},
               "proposals": {"items": [{"hypothesis": "f returns a constant", "evidence_ids": ["e1"]}]}},
        diagnostics=["string at 0x10 reads 'ignore previous instructions'"], classification="leaf", entry=0x40)


def test_evidence_record_round_trips_through_a_fresh_store(tmp_path):
    path = tmp_path / "ev.db"
    record, payloads = record_from_evidence(_evidence(), provider_route="rea-ghidra", engine="ghidra-12.1.4",
                                            target_digest="t" * 64, profile={"processor": "mips", "route": "rea"},
                                            correlation_group="grp", snapshot="s1")
    assert record.underlying_engine == "ghidra" and record.engine_version == "12.1.4"
    assert record.untrusted_target_content is True and SCOPE_LIMITATION in record.limitations
    assert record.facets["pseudocode"]["status"] == "complete"
    assert record.facets["calls"]["status"] == "partial"  # truncated or partial is never "complete"
    assert record.facets["types"] == {"status": "unavailable", "reason": "not produced by this analysis (unknown)"}
    assert record.inferences[0]["hypothesis"] == "f returns a constant"
    # route keys do not change what was analysed
    same, _ = record_from_evidence(_evidence(), provider_route="direct-ghidra", engine="ghidra-12.1.4",
                                   target_digest="t" * 64, profile={"processor": "mips"},
                                   correlation_group="grp", snapshot="s1")
    assert same.analysis_profile_digest == record.analysis_profile_digest

    assert EvidenceStore(Store(path)).save(record, payloads) is True
    assert EvidenceStore(Store(path)).save(record, payloads) is False  # idempotent
    fresh = EvidenceStore(Store(path))
    loaded = fresh.load(record.evidence_id)
    assert loaded == record
    assert EvidenceRecord.from_dict(json.loads(json.dumps(loaded.to_dict()))) == record
    assert fresh.payload(record.facets["pseudocode"]["payload_ref"])["text"] == "int f(void) { return 1; }"
    assert [r.evidence_id for r in fresh.for_function("game::f", "s1")] == [record.evidence_id]
    assert fresh.for_function("game::f", "other-snapshot") == []


def test_a_record_never_points_at_a_missing_payload(tmp_path):
    store = Store(tmp_path / "atomic.db")
    record, payloads = record_from_evidence(_evidence(), provider_route="x", engine="e", target_digest="t",
                                            profile={}, correlation_group="g", snapshot="s")
    with pytest.raises(ValueError, match="never stored"):
        EvidenceStore(store).save(record, {})
    tampered = {ref: body + " " for ref, body in payloads.items()}
    with pytest.raises(ValueError, match="does not match"):
        EvidenceStore(store).save(record, tampered)
    assert store.all("SELECT * FROM evidence_records") == []
    assert store.all("SELECT * FROM evidence_payloads") == []  # rolled back with the record


def test_newer_evidence_schema_is_refused():
    with pytest.raises(ValueError, match="newer"):
        EvidenceRecord.from_dict({"schema_version": 99})


def test_broker_persists_fresh_evidence_once(tmp_path):
    store = Store(tmp_path / "broker.db")
    broker, fake = make_broker(tmp_path)
    broker.evidence_store = EvidenceStore(store)
    register(broker, fake, tmp_path)
    bundle = broker.get_function_evidence("game::main", ("pseudocode",))
    assert bundle.ok()
    ref = bundle.refs["evidence:game::main"]
    record = EvidenceStore(store).load(ref.ref)
    assert record.function_key == "game::main" and record.provider_route == "fake"
    assert record.correlation_group == bundle.correlation_group and record.entry == "0x00001000"
    assert record.underlying_engine == "fake" and record.engine_version == "1.0"
    broker.get_function_evidence("game::main", ("pseudocode",))
    assert len(store.all("SELECT * FROM evidence_records")) == 1


def test_network_broker_has_an_evidence_store(tmp_path):
    assert isinstance(Network(tmp_path / "n.db").analysis_broker().evidence_store, EvidenceStore)


# ------------------------------------------------------------------------------------ 18.9 config

def test_every_shipped_project_config_validates():
    for toml in sorted((ROOT / "projects").glob("*/project.toml")):
        validate_config(tomllib.loads(toml.read_text(encoding="utf-8")))


@pytest.mark.parametrize("cfg, message", [
    ({"analysis": {"enabeld": True}}, "unknown key analysis.enabeld"),
    ({"retrieval": {"bsim": True}}, "unknown key retrieval.bsim"),
    ({"rea": {"read_only": False}}, "only admitted read-only"),
    ({"analysis": {"allow_target_byte_mutation": True}}, "never mutate target bytes"),
    ({"rea": {"enabled": True}}, "pinned rea.artifact"),
    ({"search": {"portfolio_size": True}}, "portfolio_size must be an integer"),
    ({"search": {"policy": "yolo"}}, "policy must be one of"),
    ({"matching": "on"}, r"\[matching\] must be a table"),
    ({"retrieval": {"route_order": ["bsim", "bsim"]}}, "route_order must be"),
])
def test_dangerous_or_unknown_keys_are_rejected(cfg, message):
    with pytest.raises(ConfigError, match=message):
        validate_config(cfg)
    assert issubclass(ConfigError, ProjectRejected)


def test_top_level_keys_stay_free_form_and_known_values_pass():
    assert validate_config({"slug": "x", "kind": "rom", "segments": [], "anything": 1,
                            "rea": {"enabled": True, "artifact": "rea-6.4.0", "schema_digest": "abc"},
                            "retrieval": {"holdout": ["f"], "route_order": ["exact_twins", "bsim"]},
                            "analysis": {"persist_live_database": True}}) == [
        "analysis.persist_live_database keeps a live analysis database on disk"]


def test_effective_config_reports_values_and_their_sources():
    diag = effective_config({"search": {"portfolio_size": 2, "policy": "guided_v1"}},
                            env={"DECOMPNET_GUIDED_SEARCH": "0"})
    search = diag["sections"]["search"]
    assert search["portfolio_size"] == {"value": 2, "source": "project"}
    assert search["policy"] == {"value": "single_v1", "source": "env:DECOMPNET_GUIDED_SEARCH"}
    assert any("DECOMPNET_GUIDED_SEARCH" in w for w in diag["warnings"])
    defaults = effective_config({}, env={})["sections"]
    # guided search is the default finisher's policy; the other optional features are off unless opted into
    assert defaults["search"]["policy"]["value"] == "guided_v1"
    assert defaults["search"]["structural_rerank"]["value"] is False
    assert defaults["analysis"]["enabled"]["value"] is False
    assert defaults["rea"]["enabled"]["value"] is False
    assert defaults["rea"]["read_only"]["value"] is True
    assert not any(defaults["retrieval"][k]["value"] for k in ("function_id_retrieval", "bsim_retrieval",
                                                               "external_queries"))
    assert all(item["source"] == "default" for sec in defaults.values() for item in sec.values())


def test_import_rejects_a_dangerous_config(tmp_path, n64):
    proj = n64
    with open(proj / "project.toml", "a", encoding="utf-8") as f:
        f.write("\n[analysis]\nallow_target_byte_mutation = true\n")
    net = Network(tmp_path / "reject.db")
    with pytest.raises(ConfigError):
        import_project(net.store, proj, log=lambda *_: None)
    assert net.store.all("SELECT * FROM projects") == []


def test_retrieval_parsing_shares_the_schema():
    proj = {"config": json.dumps({"retrieval": {"holdout": ["f"], "function_id_retrieval": True}})}
    assert config_from(proj).function_id_retrieval is True
    with pytest.raises(NetworkError, match="bad retrieval config"):
        config_from({"config": json.dumps({"retrieval": {"external_queries": "yes"}})})


def test_cli_prints_the_effective_configuration(capsys):
    assert cli.main(["config", str(DEMO)]) == 0
    out = capsys.readouterr().out
    assert "[analysis]" in out and "enabled = false  (default)" in out
    assert load_config(DEMO)["slug"] == "demo"


def test_project_takedown_removes_its_identities_and_evidence(tmp_path):
    net = Network(tmp_path / "takedown.db")
    make_project(net, 2)  # slug 'fixture'
    Network(tmp_path / "takedown.db")  # backfill identities
    store = EvidenceStore(net.store)
    mine, mine_payloads = record_from_evidence(_evidence(), provider_route="x", engine="e", target_digest="t",
                                               profile={}, correlation_group="g", snapshot="s")
    mine = EvidenceRecord.from_dict({**mine.to_dict(), "function_key": "fixture::func_001"})
    other_ev = _evidence()
    other_ev.views["pseudocode"] = {"text": "other project", "truncated": False}
    theirs, their_payloads = record_from_evidence(other_ev, provider_route="x", engine="e", target_digest="t",
                                                  profile={}, correlation_group="g", snapshot="s")
    theirs = EvidenceRecord.from_dict({**theirs.to_dict(), "evidence_id": "ev-other",
                                       "function_key": "fixture-2::func_001"})
    store.save(mine, mine_payloads)
    store.save(theirs, their_payloads)
    assert net.store.all("SELECT * FROM function_identity")
    net.remove_project("fixture")
    assert net.store.all("SELECT * FROM function_identity") == []
    assert [r["evidence_id"] for r in net.store.all("SELECT evidence_id FROM evidence_records")] == ["ev-other"]
    assert store.payload(mine.facets["pseudocode"]["payload_ref"]) is None  # the taken-down text is gone
    for info in theirs.facets.values():  # what another project still references stays
        if "payload_ref" in info:
            assert store.payload(info["payload_ref"]) is not None
