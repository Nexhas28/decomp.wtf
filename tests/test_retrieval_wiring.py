"""Engine wiring of the retrieval layer (plan P10.4/P10.5, Section 18.6; Workstream B).

Stub-based: a synthetic RomNetwork project, no compiler, no Ghidra (the FID/BSim lanes' toolchain
gate is stubbed exactly like tests/test_retrieval.py does). Covers:

- flags off (default): find_similar output byte-identical to the pre-retrieval brief
- flags on: Function ID / BSim candidates appended with requires_verification labels,
  in the configured route order
- the engine hook (retrieval_candidates) returns the routed list, empty when disabled
- reference_sources rows persist across a coordinator restart and round-trip through
  to_json/from_json
- unknown retrieval config keys are rejected with a clear error
- registry leakage_check is reachable from the engine
- remove_project cleans a project's reference rows (global corpus rows survive)

Retrieval never touches match state: every candidate goes through compile_and_diff like any other.
"""

from __future__ import annotations

import itertools
import json
from types import SimpleNamespace

import pytest

from decompnet import scoring, snapshots, telemetry
from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError, config_from
from decompnet.retrieval import BsimIndex, FunctionIdIndex, Registry, digest
from decompnet.retrieval import bsim as bsim_module
from decompnet.retrieval import fid as fid_module

from .test_retrieval import LINES_A, bsim_sig, fake_toolchain, fid_hit, make_registry, ref_entry, sidecar

# The fixture functions' stored assembly: same mnemonics as LINES_A, so a BSim target signature
# built from it ranks the index signature at similarity 1.0.
ASM = "\n".join(LINES_A) + "\n"


def make_project(n: RomNetwork, retrieval_block=None, n_functions: int = 2):
    cfg: dict = {}
    if retrieval_block is not None:
        cfg["retrieval"] = retrieval_block
    with n.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'fixture', 'fixture', 'retrieval fixture', 'mips', 0, '/tmp/fixture', ?)",
                   (json.dumps(cfg),))
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, 'int f(void);', NULL, 'fixture', 0, '')")
        for i in range(1, n_functions + 1):
            db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, status, "
                       "best_score, unit, vram) VALUES(?,1,?,8,2,NULL,'[]',?,'open',?, '', ?)",
                       (i, f"f{i}", ASM, 10 * i, 0x80000000 + i))
    return n.store.one("SELECT * FROM projects WHERE id=1")


@pytest.fixture
def net(tmp_path):
    n = RomNetwork(tmp_path / "retrieval.db", tmp_path)
    make_project(n)
    return n


def look_alike_row(net):
    return net.store.one("SELECT name, vram, size, n_insns, status, best_score FROM functions WHERE id=2")


def enable_retrieval(net, block):
    with net.store.tx() as db:
        db.execute("UPDATE projects SET config=? WHERE id=1", (json.dumps({"retrieval": block}),))
    return net.store.one("SELECT * FROM projects WHERE id=1")


def seed_indexes(net, monkeypatch):
    """Admit a usable registry and load fake FID/BSim indexes (toolchain gate stubbed)."""
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    registry = make_registry()
    net.save_reference_sources(registry, project_id=1)
    fid = FunctionIdIndex(registry)
    fid.add(fid_hit("h1", entry_id="liba-1", signature=snapshots.target_digest(fn)))
    net.retrieval_fid = fid
    bsim = BsimIndex(registry)
    bsim.add(bsim_sig("a", LINES_A, calls=1), sidecar("good", "liba-1"))
    net.retrieval_bsim = bsim
    fake_toolchain(monkeypatch, fid_module, ok=True)
    fake_toolchain(monkeypatch, bsim_module, ok=True)


# ----------------------------------------------------------------------------- flags off (default)

def test_flags_off_find_similar_is_byte_identical(net, monkeypatch):
    """With no `retrieval` block in the project config (the default), find_similar is exactly the
    pre-retrieval brief: no retrieval section, no requires_verification labels."""
    monkeypatch.setattr(net, "similar", lambda proj, fn, top: [(look_alike_row(net), 0.95)])
    out = net.find_similar("fixture", "f1")
    expected = ("Functions that look like f1:\n"
                "- f2  95% similar  best 20  (2 insns, 0x80000002)")
    assert out == expected
    assert "REQUIRES VERIFICATION" not in out and "Retrieval" not in out


def test_engine_hook_empty_when_disabled(net):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    assert net.retrieval_candidates(fn, "fixture") == []


# ----------------------------------------------------------------------------- flags on

def test_flags_on_append_candidates_with_verification_labels_in_route_order(net, monkeypatch):
    """FID and BSim candidates are appended to the brief with explicit requires_verification labels,
    grouped in the configured route order — and match state is never touched."""
    enable_retrieval(net, {"function_id_retrieval": True, "bsim_retrieval": True,
                           "route_order": ["exact_twins", "bsim", "function_id", "lightweight_similarity"]})
    seed_indexes(net, monkeypatch)
    monkeypatch.setattr(net, "similar", lambda proj, fn, top: [(look_alike_row(net), 0.95)])
    out = net.find_similar("fixture", "f1")
    assert out.startswith("Functions that look like f1:")
    assert "[bsim] good" in out and "[function_id] memcpy" in out
    assert "REQUIRES VERIFICATION" in out
    assert out.index("### Retrieval candidates") < out.index("[bsim]")
    assert out.index("[bsim]") < out.index("[function_id]")  # configured route order honoured
    assert net.store.one("SELECT status FROM functions WHERE id=1")["status"] == "open"
    assert net.store.one("SELECT attempts FROM functions WHERE id=1")["attempts"] == 0


def test_default_route_order_fid_before_bsim(net, monkeypatch):
    enable_retrieval(net, {"function_id_retrieval": True, "bsim_retrieval": True})
    seed_indexes(net, monkeypatch)
    monkeypatch.setattr(net, "similar", lambda proj, fn, top: [(look_alike_row(net), 0.95)])
    out = net.find_similar("fixture", "f1")
    assert out.index("[function_id]") < out.index("[bsim]")  # exact twins -> FID -> similarity -> BSim


def test_engine_hook_returns_routed_candidates(net, monkeypatch):
    enable_retrieval(net, {"function_id_retrieval": True, "bsim_retrieval": True})
    seed_indexes(net, monkeypatch)
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    cands = net.retrieval_candidates(fn, "fixture")
    assert {c["route"] for c in cands} == {"function_id", "bsim"}
    assert all(c["requires_verification"] is True for c in cands)
    assert all(c["candidate"].is_match is False for c in cands)  # retrieval never matches anything


def test_flags_on_without_admitted_corpus_stays_disabled_with_reason(net, monkeypatch):
    """Flags on but no usable registry entry: the routes are refused by the admission gate and the
    brief explains why instead of silently querying nothing."""
    enable_retrieval(net, {"function_id_retrieval": True, "bsim_retrieval": True})
    monkeypatch.setattr(net, "similar", lambda proj, fn, top: [(look_alike_row(net), 0.95)])
    out = net.find_similar("fixture", "f1")
    assert "### Retrieval routes (disabled)" in out
    assert "usable" in out  # the admission reason is shown
    assert "REQUIRES VERIFICATION" not in out
    assert net.retrieval_candidates(net.store.one("SELECT * FROM functions WHERE id=1"), "fixture") == []


# ----------------------------------------------------------------------------- verification (P10.2/P10.3 step 6)

class _Compiled:
    ok = True
    log = ""
    obj = b"obj"


def stub_judge(net, monkeypatch, score=40):
    """The compile/evaluate/diff judge stubbed (no toolchain): a non-matching verdict, so the attempt is
    recorded without touching match state. Returns the sources that reached the compiler."""
    compiled: list = []
    monkeypatch.setattr(net, "_compile", lambda proj, header, source, fn=None: (compiled.append(source) or _Compiled()))
    monkeypatch.setattr(net, "_evaluate", lambda fn, proj, obj: (object(), None, []))
    monkeypatch.setattr(net, "_target_obj", lambda fn, proj: object())
    monkeypatch.setattr(scoring, "diff", lambda target, cand: scoring.DiffResult(score, 100, [], False))
    return compiled


def write_reference_source(net, tmp_path, text="int f(void) { return 1; }\n"):
    """The registry entry's permitted source file, under the project root."""
    path = tmp_path / "liba" / "liba-1" / "src.c"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    with net.store.tx() as db:
        db.execute("UPDATE projects SET root=? WHERE id=1", (str(tmp_path),))
    return text


def test_verify_retrieval_candidates_does_nothing_when_retrieval_is_off(net, monkeypatch, tmp_path):
    seed_indexes(net, monkeypatch)
    write_reference_source(net, tmp_path)
    compiled = stub_judge(net, monkeypatch)
    agent = net.authenticate(net.register_agent("alice"))
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    assert net.verify_retrieval_candidates(agent, proj, fn) == []
    assert compiled == []
    assert net.store.one("SELECT COUNT(*) AS n FROM attempts")["n"] == 0


def test_verify_retrieval_candidates_sends_a_candidate_through_the_judge(net, monkeypatch, tmp_path):
    enable_retrieval(net, {"function_id_retrieval": True})
    seed_indexes(net, monkeypatch)
    text = write_reference_source(net, tmp_path)
    compiled = stub_judge(net, monkeypatch)
    agent = net.authenticate(net.register_agent("alice"))
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    out = net.verify_retrieval_candidates(agent, proj, fn)
    assert compiled == [text]
    assert [(r["route"], r["matched"], r["score"]) for r in out] == [("function_id", False, 40)]
    assert out[0]["candidate"].is_match is False
    attempt = net.store.one("SELECT id, via, source FROM attempts WHERE function_id=1")
    assert attempt["via"] == "retrieval:function_id" and attempt["source"] == text
    # the snapshot was captured before the compile, and the claim was held only while compiling
    assert net.store.one("SELECT COUNT(*) AS n FROM attempt_snapshots WHERE attempt_id=?", attempt["id"])["n"] == 1
    assert net.store.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0
    assert net.store.one("SELECT status FROM functions WHERE id=1")["status"] == "open"


def test_verify_retrieval_candidates_skips_candidates_without_accessible_source(net, monkeypatch, tmp_path):
    enable_retrieval(net, {"function_id_retrieval": True})
    seed_indexes(net, monkeypatch)  # the registry's source file is never written
    compiled = stub_judge(net, monkeypatch)
    agent = net.authenticate(net.register_agent("alice"))
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    assert net.retrieval_candidates(fn, "fixture")  # there is a candidate, but no C to compile
    assert net.verify_retrieval_candidates(agent, proj, fn) == []
    assert compiled == []


def test_autopilot_retrieval_step_runs_only_when_a_route_is_enabled(net, monkeypatch):
    agent = net.authenticate(net.register_agent("alice"))
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    calls: list = []
    monkeypatch.setattr(net, "verify_retrieval_candidates",
                        lambda agent, proj, fn, limit=2: (calls.append(fn["name"]) or [{"matched": False}]))
    assert net._autopilot_retrieval(agent, proj, 1) is False
    assert calls == []  # off by default: never consulted
    enable_retrieval(net, {"bsim_retrieval": True})
    assert net._autopilot_retrieval(agent, proj, 1) is False  # consulted, nothing matched
    assert calls == ["f1"]


# ----------------------------------------------------------------------------- BSim cost (P10.3 step 7)

def test_bsim_records_build_and_query_timings(monkeypatch):
    ticks = itertools.count()
    monkeypatch.setattr(bsim_module, "time", SimpleNamespace(monotonic=lambda: next(ticks) * 0.5))
    registry = make_registry()
    index = BsimIndex(registry)
    index.add(bsim_sig("a", LINES_A, calls=1), sidecar("good", "liba-1"))
    assert index.build_seconds == 0.5
    index.query(bsim_sig("t", LINES_A, calls=1))
    stats = index.stats()
    assert stats["queries"] == 1 and stats["query_seconds_total"] == 0.5
    assert stats["query_seconds_mean"] == 0.5 and stats["build_seconds"] == 0.5 and stats["signatures"] == 1


def test_bsim_stages_appear_in_telemetry():
    index = BsimIndex(make_registry())
    telemetry.enable()
    try:
        telemetry.clear()
        index.add(bsim_sig("a", LINES_A, calls=1), sidecar("good", "liba-1"))
        index.query(bsim_sig("t", LINES_A, calls=1))
        stages = telemetry.snapshot()["stage_ms"]
    finally:
        telemetry.enable(False)
    assert stages["retrieval.bsim.build"]["n"] == 1 and stages["retrieval.bsim.query"]["n"] == 1


# ----------------------------------------------------------------------------- persistence

def test_reference_rows_persist_across_restart_and_roundtrip(net, tmp_path):
    registry = make_registry(("liba-1", "liba-2"))
    registry.add(ref_entry("liba-3", holdout=True))
    net.save_reference_sources(registry, project_id=1)
    loaded = net.load_reference_sources(1)
    assert loaded.identity() == registry.identity()
    assert loaded.revision == registry.revision
    assert loaded.get("liba-3").holdout is True  # the holdout marker survives the round-trip
    assert Registry.from_json(loaded.to_json()).identity() == registry.identity()
    # a coordinator restart reads the same rows back
    path = net.store.conn.execute("PRAGMA database_list").fetchone()["file"]
    reopened = RomNetwork(path, tmp_path)
    again = reopened.load_reference_sources(1)
    assert again.identity() == registry.identity()
    assert again.revision == registry.revision


def test_re_saving_a_project_registry_replaces_its_rows(net):
    net.save_reference_sources(make_registry(("liba-1", "liba-2")), project_id=1)
    net.save_reference_sources(make_registry(("liba-3",)), project_id=1)
    loaded = net.load_reference_sources(1)
    assert [e.entry_id for e in loaded] == ["liba-3"]


def test_remove_project_cleans_its_reference_rows(net):
    net.save_reference_sources(make_registry(("liba-1",)), project_id=1)
    net.save_reference_sources(make_registry(("global-1",)), project_id=None)
    net.remove_project("fixture")
    assert [e.entry_id for e in net.load_reference_sources()] == ["global-1"]  # the shared corpus survives


# ----------------------------------------------------------------------------- config parsing

def test_unknown_retrieval_config_keys_rejected(net):
    with net.store.tx() as db:
        db.execute("UPDATE projects SET config=? WHERE id=1",
                   (json.dumps({"retrieval": {"function_id_retrieval": True, "remote_index": "x"}}),))
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    with pytest.raises(NetworkError, match="unknown retrieval config key"):
        config_from(proj)
    with pytest.raises(NetworkError, match="unknown retrieval config key"):
        net.retrieval_candidates(net.store.one("SELECT * FROM functions WHERE id=1"), "fixture")


def test_invalid_route_order_in_config_rejected(net):
    with net.store.tx() as db:
        db.execute("UPDATE projects SET config=? WHERE id=1",
                   (json.dumps({"retrieval": {"route_order": ["remote_index"]}}),))
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    with pytest.raises(NetworkError, match="bad retrieval config"):
        config_from(proj)


# ----------------------------------------------------------------------------- leakage (P10.5)

def test_leakage_check_reachable_from_the_engine(net):
    net.save_reference_sources(make_registry(("liba-1",)), project_id=1)
    leaks = net.retrieval_leakage_check("fixture", {
        "q1": {"source_digest": digest({"src": "liba-1", "version": "1.0"}),
               "target_digest": digest({"obj": "liba-1"}),
               "source_location": "liba/liba-1/src.c"}})
    assert {leak.kind for leak in leaks} == {"source_digest", "target_digest", "source_location"}
    assert all(leak.entry_id == "liba-1" and leak.answer_id == "q1" for leak in leaks)
