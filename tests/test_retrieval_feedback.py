"""A.14 — P6.5 acceptance-feedback wiring (on_match + find_similar) and the P10.5 surface gate.

Red-first coverage of the wiring contract:

- when a function matches, on_match marks its similarity-index entry source-available and records the
  retrieval route its verification receipt names (exact twins / similarity / FID / BSim);
- plain agent/room origins mark availability without inventing a retrieval route;
- find_similar runs the holdout leakage check against the admitted registry before any retrieval
  candidate is surfaced, and blocks the surface when the function is a leaked answer;
- with every retrieval flag off (the default) find_similar output is byte-identical to the
  pre-retrieval brief;
- the wiring never touches match state (status/attempts/best all belong to the judge).

Stub-based like tests/test_retrieval_wiring.py: a synthetic RomNetwork project, no compiler needed.
"""

from __future__ import annotations

import json

import pytest

from decompnet import similarity, snapshots
from decompnet.app.engine import RomNetwork
from decompnet.retrieval import ReferenceEntry, Registry, digest
from decompnet.retrieval import feedback
from decompnet.retrieval.feedback import route_from_origin, surface_leaks

from .test_retrieval import make_registry, ref_entry
from .test_retrieval_wiring import ASM, enable_retrieval, look_alike_row, make_project, seed_indexes


def leaking_entry(entry_id: str, source_digest: str) -> ReferenceEntry:
    """A corpus entry whose source digest equals a benchmark answer's source digest (P10.5/B-05)."""
    return ReferenceEntry(entry_id=entry_id, repository="liba", version="1.0",
                          source_location=f"liba/{entry_id}/answer.c", toolchain="gcc-2.7.2-kmc",
                          profile="gcc-2.7.2-kmc", target_identity=digest({"obj": entry_id}),
                          processor="mips", mode="", provenance="built in CI",
                          source_digest=source_digest, holdout=False)

# a real 4-instruction MIPS function: addiu sp,-0x18; sw ra,0x14(sp); jr ra; nop
MIPS_BYTES = bytes.fromhex("27bdffe8 afbf0014 03e00008 00000000")


@pytest.fixture
def net(tmp_path):
    n = RomNetwork(tmp_path / "feedback.db", tmp_path)
    make_project(n)
    return n


@pytest.fixture(autouse=True)
def clear_reuse_ledger():
    """similarity.note_reuse's ledger is in-memory module state: isolate it per test."""
    similarity._ledger.clear()
    yield
    similarity._ledger.clear()


@pytest.fixture
def proj(net):
    return net.store.one("SELECT * FROM projects WHERE id=1")


def write_receipt(net, fid: int, origin: str) -> None:
    with net.store.tx() as db:
        db.execute(
            "INSERT OR REPLACE INTO verification_receipts(function_id, attempt_id, snapshot_id, source_digest, "
            "candidate_object_digest, origin, object_match, function_link_match, unit_code_data_match, "
            "whole_image_checksum, acceptance_policy, verified_at_utc) "
            "VALUES(?,NULL,'snap','src',NULL,?,'exact','exact','unverified','not_run','rom-function-policy-v1','now')",
            (fid, origin))


def fire_on_match(net, proj, fid: int, monkeypatch) -> None:
    """Call on_match without the twins background pass (its threads are out of scope here)."""
    monkeypatch.setattr(net.twins, "after_match", lambda proj, fid: None)
    net.on_match(proj, fid)


def register_answer(net, fn, answer_id: str = "q1", identities: dict | None = None) -> None:
    identities = identities or {}
    net._holdout_answers = {1: {
        answer_id: {
            "source_digest": identities.get("source_digest", ""),
            "target_digest": identities.get("target_digest", snapshots.target_digest(fn)),
            "source_location": identities.get("source_location", ""),
        }}}


# ---------------------------------------------------------------------------- route mapping

def test_route_from_origin_maps_the_four_routes_and_synonyms():
    assert route_from_origin("function_id") == "function_id"
    assert route_from_origin("fid") == "function_id"
    assert route_from_origin("bsim") == "bsim"
    assert route_from_origin("lightweight_similarity") == "lightweight_similarity"
    assert route_from_origin("similarity") == "lightweight_similarity"
    assert route_from_origin("lookalike") == "lightweight_similarity"
    assert route_from_origin("exact_twins") == "exact_twins"
    assert route_from_origin("exact-twin") == "exact_twins"  # twins.py's historical spelling
    assert route_from_origin("twin") == "exact_twins"
    # plain match origins are not retrieval routes
    assert route_from_origin("agent") is None
    assert route_from_origin("room") is None
    assert route_from_origin("permuter") is None
    assert route_from_origin("") is None
    assert route_from_origin("not-a-route") is None


# ---------------------------------------------------------------------------- on_match wiring

def test_on_match_marks_index_available_and_records_fid_route(net, proj, monkeypatch):
    """A receipt whose origin names a retrieval route: the index entry is marked source-available and
    the reuse ledger records the canonical route (A.14 #1/#2)."""
    write_receipt(net, 1, "function_id")
    fire_on_match(net, proj, 1, monkeypatch)
    index = net._similarity_index
    assert index is not None and isinstance(index, similarity.FeatureIndex)
    stats = index.stats()
    assert stats["available_sources"] == 1
    assert stats["reuse_routes"] == {1: "function_id"}
    assert similarity.reuse_stats(proj["id"])["routes"]["function_id"] == 1
    assert "lightweight_similarity" not in similarity.reuse_stats(proj["id"])["routes"]


def test_on_match_builds_the_index_entry_from_target_bytes(net, proj, monkeypatch):
    """With real target bytes the matched function gets a features entry (P6.2), so acceptance can
    mark *its index entry* — not just the ledger."""
    with net.store.tx() as db:
        db.execute("UPDATE functions SET target=?, vram=0x80001000, size=16, n_insns=4 WHERE id=1",
                   (MIPS_BYTES,))
    write_receipt(net, 1, "bsim")
    fire_on_match(net, proj, 1, monkeypatch)
    index = net._similarity_index
    stats = index.stats()
    assert stats["entries"] == 1
    assert stats["available_sources"] == 1
    assert stats["reuse_routes"] == {1: "bsim"}
    assert similarity.reuse_stats(proj["id"])["routes"]["bsim"] == 1


def test_on_match_marks_availability_for_plain_agent_matches_without_a_retrieval_route(net, proj, monkeypatch):
    """A receipt with a plain `agent` origin still marks the source available (P6.5 #1/#3) but never
    invents a retrieval route: none of the four route keys appear in the ledger."""
    write_receipt(net, 1, "agent")
    fire_on_match(net, proj, 1, monkeypatch)
    index = net._similarity_index
    assert index.stats()["available_sources"] == 1
    routes = similarity.reuse_stats(proj["id"])["routes"]
    for route in ("exact_twins", "lightweight_similarity", "function_id", "bsim"):
        assert route not in routes


def test_on_match_records_exact_twins_origin(net, proj, monkeypatch):
    write_receipt(net, 1, "exact-twin")
    fire_on_match(net, proj, 1, monkeypatch)
    assert net._similarity_index.stats()["reuse_routes"] == {1: "exact_twins"}
    assert similarity.reuse_stats(proj["id"])["routes"]["exact_twins"] == 1


def test_on_match_without_a_receipt_does_nothing(net, proj, monkeypatch):
    fire_on_match(net, proj, 1, monkeypatch)  # no receipt row at all
    assert getattr(net, "_similarity_index", None) is None
    assert similarity.reuse_stats(proj["id"])["records"] == 0


def test_on_match_never_touches_match_state(net, proj, monkeypatch):
    """The judge owns status/attempts/best: the acceptance wiring only reads the receipt and updates
    diagnostics (A.14)."""
    write_receipt(net, 1, "function_id")
    before = dict(net.store.one("SELECT status, attempts, best_score FROM functions WHERE id=1"))
    fire_on_match(net, proj, 1, monkeypatch)
    after = dict(net.store.one("SELECT status, attempts, best_score FROM functions WHERE id=1"))
    assert after == before
    assert net.store.one("SELECT 1 FROM claims WHERE function_id=1") is None


# ---------------------------------------------------------------------------- find_similar gate

def test_find_similar_blocks_a_leaked_answer_before_surfacing(net, monkeypatch):
    """Retrieval routes enabled, the admitted corpus leaks a registered holdout answer, and the
    queried function is that answer: no retrieval candidate is surfaced, with the reason shown
    (A.14 #3 / P10.5)."""
    enable_retrieval(net, {"function_id_retrieval": True})
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    registry = Registry()
    registry.add(leaking_entry("liba-1", digest({"src": "answer.c"})))
    net.save_reference_sources(registry, project_id=1)
    monkeypatch.setattr(net, "similar", lambda proj, fn, top: [(look_alike_row(net), 0.95)])
    register_answer(net, fn, identities={
        "source_digest": digest({"src": "answer.c"}),
        "target_digest": snapshots.target_digest(fn),
        "source_location": "liba/answer.c",
    })
    out = net.find_similar("fixture", "f1")
    assert "blocked (holdout leakage)" in out
    assert "REQUIRES VERIFICATION" not in out      # no candidate lines were surfaced
    assert "[function_id]" not in out
    assert out.startswith("Functions that look like f1:")  # the local similarity brief stays


def test_find_similar_surfaces_normally_when_the_registry_is_clean(net, monkeypatch):
    """Flags on, registry admitted, no registered answers: candidates surface with their
    requires-verification labels, exactly as before this change."""
    enable_retrieval(net, {"function_id_retrieval": True})
    seed_indexes(net, monkeypatch)
    monkeypatch.setattr(net, "similar", lambda proj, fn, top: [(look_alike_row(net), 0.95)])
    out = net.find_similar("fixture", "f1")
    assert "REQUIRES VERIFICATION" in out
    assert "[function_id] memcpy" in out
    assert "blocked" not in out


def test_find_similar_with_flags_off_stays_byte_identical_even_with_answers(net, monkeypatch):
    """With every retrieval flag off, the surface is the exact pre-retrieval brief — registered
    holdout answers and admitted corpus rows must not change a byte of it."""
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    net.save_reference_sources(make_registry(("liba-1",)), project_id=1)
    register_answer(net, fn)
    monkeypatch.setattr(net, "similar", lambda proj, fn, top: [(look_alike_row(net), 0.95)])
    out = net.find_similar("fixture", "f1")
    expected = ("Functions that look like f1:\n"
                "- f2  95% similar  best 20  (2 insns, 0x80000002)")
    assert out == expected
    assert "REQUIRES VERIFICATION" not in out and "Retrieval" not in out


def test_surface_leaks_is_empty_when_retrieval_is_disabled(net):
    """The gate never consults the registry while the routes are off (A.14: byte-identical flags-off)."""
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    config = __import__("decompnet.retrieval.registry", fromlist=["RetrievalConfig"]).RetrievalConfig()
    assert surface_leaks(net, net.store.one("SELECT * FROM projects WHERE id=1"), fn, config) == []


def test_find_similar_leakage_check_never_touches_match_state(net, monkeypatch):
    enable_retrieval(net, {"function_id_retrieval": True})
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    registry = Registry()
    registry.add(leaking_entry("liba-1", digest({"src": "answer.c"})))
    net.save_reference_sources(registry, project_id=1)
    monkeypatch.setattr(net, "similar", lambda proj, fn, top: [(look_alike_row(net), 0.95)])
    register_answer(net, fn, identities={"source_digest": digest({"src": "answer.c"})})
    before = dict(net.store.one("SELECT status, attempts, best_score FROM functions WHERE id=1"))
    net.find_similar("fixture", "f1")
    after = dict(net.store.one("SELECT status, attempts, best_score FROM functions WHERE id=1"))
    assert after == before
