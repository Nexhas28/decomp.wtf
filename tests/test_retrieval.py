"""Retrieval tests (plan B-01..B-06, P10.1-P10.5; PR-27/PR-28).

Pure python + fake (in-memory/JSON) indexes. The real Function ID / BSim lanes are gated on
analyzeHeadless plus a Java runtime and skip with an explicit reason on hosts without them; the fake
indexes here exercise the full gating logic with no external tool or network use.
"""

from dataclasses import replace

import pytest

from decompnet.retrieval import (BsimIndex, BsimLane, BsimSignature, FidError, FidLane, FunctionIdHit,
                                 FunctionIdIndex, ReferenceEntry, Registry, RetrievalConfig,
                                 SidecarEntry, admit_retrieval, build_candidate, digest,
                                 enabled_routes, normalise_instructions, route_plan,
                                 toolchain_available)
from decompnet.retrieval import bsim as bsim_module
from decompnet.retrieval import fid as fid_module

DEFAULT_ORDER = ("exact_twins", "function_id", "lightweight_similarity", "bsim")


# ---------------------------------------------------------------------------- fixtures

def ref_entry(entry_id, *, repository="liba", version="1.0", profile="gcc-2.7.2-kmc",
              toolchain=None, target=None, processor="mips", mode="", **kw) -> ReferenceEntry:
    """A registry reference entry whose digests derive from its id by default (B-04 tests override)."""
    return ReferenceEntry(entry_id=entry_id, repository=repository, version=version,
                          source_location=f"liba/{entry_id}/src.c",
                          source_digest=digest({"src": entry_id, "version": version}),
                          toolchain=toolchain or profile, profile=profile,
                          target_identity=target or digest({"obj": entry_id}),
                          processor=processor, mode=mode, provenance="built in CI", **kw)


def make_registry(ids=("liba-1",)) -> Registry:
    return Registry([ref_entry(i) for i in ids])


def fid_hit(hit_id, *, name="memcpy", signature="sig", entry_id="", version="1.0",
            corpus_version="gcc-2.7.2-kmc", processor="mips", mode="", profile="gcc-2.7.2-kmc",
            target_digest=None) -> FunctionIdHit:
    return FunctionIdHit(hit_id=hit_id, name=name, library="liba", repository="liba", version=version,
                         corpus_version=corpus_version, processor=processor, mode=mode,
                         signature=signature, entry_id=entry_id, profile=profile,
                         target_digest=target_digest or (digest({"obj": entry_id}) if entry_id else ""))


def sidecar(key, entry_id, *, version="1.0", profile="gcc-2.7.2-kmc", toolchain=None,
            program_digest=None, **kw) -> SidecarEntry:
    return SidecarEntry(candidate_key=key, entry_id=entry_id,
                        source_location=f"liba/{entry_id}/src.c",
                        compiler_profile=profile, toolchain=toolchain or profile, version=version,
                        program_digest=program_digest or (digest({"obj": entry_id}) if entry_id else ""),
                        **kw)


def bsim_sig(sig_id, lines, *, profile="gcc-2.7.2-kmc", program_digest=None, **kw) -> BsimSignature:
    return BsimSignature.from_lines(sig_id, lines, processor="mips", mode="", profile=profile,
                                    program_digest=program_digest or digest({"obj": sig_id}), **kw)


LINES_A = ["lwz r3, 0x1234(r31)", "addi r4, r3, 1", "stw r4, 0x5678(r31)", "blr"]
LINES_B = ["lwz r3, 0x9000(r31)", "addi r4, r3, 7", "stw r4, 0x8000(r31)", "blr"]


def fake_toolchain(monkeypatch, module, ok=True, reason="fake toolchain"):
    monkeypatch.setattr(module, "toolchain_available", lambda: (ok, reason))


# ---------------------------------------------------------------------------- P10.1 registry

def test_registry_versioned_serializable_roundtrip(tmp_path):
    registry = make_registry()
    assert registry.revision == 0
    registry.add(ref_entry("liba-2"))
    assert registry.revision == 1
    first_identity = registry.identity()
    text = registry.to_json()
    loaded = Registry.from_json(text)
    assert loaded.identity() == first_identity
    assert loaded.revision == registry.revision
    assert len(loaded) == len(registry)
    assert loaded.get("liba-1") == registry.get("liba-1")
    # the explicit load/save API (Lead wires persistence into the DB behind this surface)
    path = registry.save(tmp_path / "registry.json")
    assert Registry.load(path).identity() == first_identity
    registry.remove("liba-2")
    assert registry.revision == 2
    assert Registry.load(path).identity() != registry.identity()


def test_registry_identity_changes_on_corpus_change():
    registry = make_registry()
    before = registry.identity()
    registry.replace(ref_entry("liba-1", version="2.0"))
    assert registry.identity() != before
    assert registry.revision == 1


def test_holdout_marker_excluded_from_usable_entries():
    registry = Registry([ref_entry("liba-1"), ref_entry("holdout-1", holdout=True),
                         ref_entry("denied-1", usage_restrictions=("source-denied",))])
    usable = registry.usable_entries()
    assert [e.entry_id for e in usable] == ["liba-1"]
    admission = admit_retrieval(RetrievalConfig(function_id_retrieval=True), registry)
    assert admission.ok()


# ---------------------------------------------------------------------------- P10.5 leakage (B-05)

def test_holdout_leakage_detector_flags_answer_in_corpus():
    registry = Registry([ref_entry("liba-1"), ref_entry("liba-2")])
    answers = {"q1": {"source_digest": digest({"src": "liba-1", "version": "1.0"}),
                      "target_digest": digest({"obj": "liba-1"}),
                      "source_location": "liba/liba-1/src.c"}}
    leaks = registry.leakage_check(answers)
    assert {leak.kind for leak in leaks} == {"source_digest", "target_digest", "source_location"}
    assert all(leak.entry_id == "liba-1" and leak.answer_id == "q1" for leak in leaks)
    assert not registry.leakage_check({"q2": {"source_digest": digest({"src": "other"})}})
    # a correctly holdout-marked copy is separated, not a leak
    separated = Registry([ref_entry("holdout-1", holdout=True)])
    assert not separated.leakage_check({"q1": {"target_digest": digest({"obj": "holdout-1"})}})


# ---------------------------------------------------------------------------- Function ID (P10.2)

def test_fid_library_hit_ambiguity_preserved_across_compiler_variants():  # B-01
    registry = Registry([ref_entry("liba-1", profile="gcc-2.7.2-kmc"),
                         ref_entry("liba-2", profile="gcc-2.8.1"),
                         ref_entry("liba-3", profile="mwcc-1.2")])
    index = FunctionIdIndex(registry)
    index.add(fid_hit("h1", entry_id="liba-1", profile="gcc-2.7.2-kmc",
                      corpus_version="gcc-2.7.2-kmc"))
    index.add(fid_hit("h2", entry_id="liba-2", profile="gcc-2.8.1", corpus_version="gcc-2.8.1"))
    index.add(fid_hit("h3", entry_id="liba-3", profile="mwcc-1.2", corpus_version="mwcc-1.2"))
    index.add(fid_hit("h4", name="memcpy_fast", entry_id="liba-1", profile="gcc-2.7.2-kmc",
                      corpus_version="gcc-2.7.2-kmc"))  # a conflicting name: kept, never collapsed
    result = index.lookup("sig", "mips", "")
    assert result.status == "ok"
    assert len(result.candidates) == 4
    assert result.ambiguous is True
    assert {c.hit.corpus_version for c in result.candidates} == \
        {"gcc-2.7.2-kmc", "gcc-2.8.1", "mwcc-1.2"}
    for candidate in result.candidates:  # processor/mode + versions survive candidate selection
        assert candidate.hit.processor == "mips" and candidate.hit.mode == ""
        assert candidate.hit.matched is False and candidate.is_match is False
        assert candidate.requires_verification is True
    # the JSON fixture round-trips the ambiguity too
    restored = FunctionIdIndex.from_json(index.to_json(), registry)
    assert len(restored.lookup("sig", "mips", "").candidates) == 4


def test_fid_names_types_only_through_controlled_construction():  # P10.2.5
    hit = fid_hit("h1")
    candidate = build_candidate(hit, ref_entry("liba-1"), suggested_name="memcpy",
                                suggested_types=("u32", "s32 *"))
    assert candidate.suggested_name == "memcpy" and candidate.suggested_types == ("u32", "s32 *")
    assert candidate.is_match is False and hit.matched is False
    with pytest.raises(FidError):
        build_candidate(hit, suggested_name="int memcpy(void)")  # arbitrary text never adapted
    with pytest.raises(FidError):
        build_candidate(hit, suggested_types=("123",))
    adapted = build_candidate(hit, ref_entry("liba-1"), suggested_name="memcpy")
    renamed = fid_module.adapt_candidate(adapted, suggested_name="memcpy_impl", suggested_types=("u8",))
    assert renamed.suggested_name == "memcpy_impl" and renamed.is_match is False
    assert adapted.suggested_name == "memcpy"  # the original candidate is untouched


def test_fid_entry_without_source_mapping_is_evidence_only():  # B-03
    index = FunctionIdIndex(make_registry())
    index.add(fid_hit("h1", entry_id=""))
    result = index.lookup("sig", "mips", "")
    assert result.status == "ok" and len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.evidence_only is True
    assert candidate.source_available is False and candidate.source_location == ""
    assert candidate.entry is None and candidate.is_match is False  # never fictitious reusable C


def test_fid_corpus_version_change_invalidates():  # B-04
    registry = make_registry()
    index = FunctionIdIndex(registry)
    index.add(fid_hit("h1", entry_id="liba-1"))
    assert index.lookup("sig", "mips", "").status == "ok"
    registry.replace(ref_entry("liba-1", version="2.0"))  # corpus version bump
    result = index.lookup("sig", "mips", "")
    assert result.status == "empty" and result.stale == 1
    assert "stale" in result.reason
    registry.replace(ref_entry("liba-1", version="1.0", target=digest({"obj": "liba-1", "rev": 2})))
    result = index.lookup("sig", "mips", "")
    assert result.status == "empty" and index.stale_reason("h1") == "target identity changed"
    registry.replace(ref_entry("liba-1", profile="gcc-2.8.1"))  # provider profile change
    result = index.lookup("sig", "mips", "")
    assert result.status == "empty" and index.stale_reason("h1").startswith("provider profile changed")
    registry.replace(ref_entry("liba-1"))  # restoring the admitted corpus re-validates the hit
    assert index.lookup("sig", "mips", "").status == "ok"


# ---------------------------------------------------------------------------- BSim (P10.3/P10.4)

def test_bsim_omits_constants_types_and_is_not_an_exact_judge():  # P10.3, module invariant
    assert normalise_instructions(LINES_A) == normalise_instructions(LINES_B)
    a = bsim_sig("a", LINES_A, calls=1)
    b = bsim_sig("b", LINES_B, calls=1)
    assert a.cosine(b) == 1.0  # histograms identical: constants/registers omitted by design


def test_bsim_close_constants_types_rank_close_judge_rejects():  # B-02
    registry = Registry([ref_entry("liba-1"), ref_entry("liba-2", profile="mwcc-1.2",
                                                        toolchain="mwcc-1.2")])
    index = BsimIndex(registry)
    index.add(bsim_sig("a", LINES_A, program_digest=digest({"obj": "liba-1"}), calls=1),
              sidecar("good", "liba-1"))
    index.add(bsim_sig("b", LINES_B, profile="mwcc-1.2", program_digest=digest({"obj": "liba-2"}),
                       calls=1),
              sidecar("wrong", "liba-2", profile="mwcc-1.2"))
    target = bsim_sig("target", LINES_A, calls=1)
    result = index.query(target, top=2)
    assert result.status == "ok" and len(result.candidates) == 2
    by_key = {c.key: c for c in result.candidates}
    assert by_key["good"].similarity >= 0.95 and by_key["wrong"].similarity >= 0.95
    assert abs(by_key["good"].rank_score - by_key["wrong"].rank_score) <= 0.05  # BSim ranks them close
    # the coordinator's compile-and-verify judge (mocked) compares constants/types and rejects the
    # wrong source; retrieval itself never marks anything matched
    def coordinator_judge(candidate):
        return candidate.key == "good"
    assert coordinator_judge(by_key["good"]) is True
    assert coordinator_judge(by_key["wrong"]) is False
    assert by_key["good"].is_match is False and by_key["wrong"].is_match is False


def test_bsim_entry_without_source_mapping_is_evidence_not_reusable():  # B-03
    registry = make_registry()
    index = BsimIndex(registry)
    index.add(bsim_sig("a", LINES_A, calls=1), sidecar("orphan", ""))  # no source mapping at all
    index.add(bsim_sig("b", LINES_B, calls=1),
              sidecar("binary-only", "liba-1", source_available=False))
    registry.add(ref_entry("liba-1", source_available=False))  # binary reference only
    result = index.query(bsim_sig("target", LINES_A, calls=1), top=5)
    assert len(result.evidence_only) == 2
    assert not result.candidates  # the reusable shortlist is filtered by usable source/profile
    for candidate in result.evidence_only:
        assert candidate.evidence_only is True and candidate.is_match is False
        assert candidate.source_location == ""  # never fictitious reusable C
        assert "investigation evidence" in candidate.notes[0]


def test_bsim_corpus_version_change_invalidates():  # B-04
    registry = make_registry()
    index = BsimIndex(registry)
    index.add(bsim_sig("a", LINES_A, calls=1), sidecar("good", "liba-1"))
    assert len(index.query(bsim_sig("target", LINES_A, calls=1)).candidates) == 1
    registry.replace(ref_entry("liba-1", version="2.0"))
    result = index.query(bsim_sig("target", LINES_A, calls=1))
    assert result.status == "empty" and not result.candidates
    assert index.stale_reason("a") == "corpus version changed (1.0 -> 2.0)"
    assert result.excluded[0]["reason"].startswith("corpus version changed")
    registry.replace(ref_entry("liba-1", profile="gcc-2.8.1"))  # provider profile change
    index.query(bsim_sig("target", LINES_A, calls=1))
    assert index.stale_reason("a").startswith("provider profile changed")
    registry.replace(ref_entry("liba-1"))  # restoring the admitted corpus re-validates the signature
    assert len(index.query(bsim_sig("target", LINES_A, calls=1)).candidates) == 1


def test_bsim_bounded_shortlist_filtered_and_reranked():  # P10.3.5
    registry = Registry([ref_entry("liba-1"),
                         ref_entry("liba-2", profile="mwcc-1.2", toolchain="mwcc-1.2"),
                         ref_entry("liba-3", profile="mwcc-1.2", toolchain="mwcc-1.2"),
                         ref_entry("liba-4", profile="gcc-2.8.1", toolchain="gcc-2.8.1")])
    index = BsimIndex(registry)
    index.add(bsim_sig("a", LINES_A, calls=1), sidecar("profile-match", "liba-1"))
    index.add(bsim_sig("b", LINES_A, calls=1, profile="gcc-2.8.1"),
              sidecar("verified", "liba-4", profile="gcc-2.8.1"))
    index.add(bsim_sig("c", LINES_A, calls=1), sidecar("plain", "liba-2", profile="mwcc-1.2"))
    index.add(bsim_sig("d", LINES_A, calls=1), sidecar("orphan", ""))  # evidence-only, not in shortlist
    target = bsim_sig("target", LINES_A, calls=1)
    result = index.query(target, top=3, project_features={"verified_functions": {"verified"}})
    assert [c.key for c in result.candidates] == ["profile-match", "verified", "plain"]
    assert result.candidates[0].rank_score == 1.03  # similarity 1.0 + matching profile bonus
    assert result.candidates[1].rank_score == 1.02  # similarity 1.0 + prior verification bonus
    assert len(result.candidates) == 3  # bounded shortlist
    assert [c.key for c in result.evidence_only] == ["orphan"]
    assert "compiler profile matches the target" in result.candidates[0].notes


def test_bsim_index_json_fixture_roundtrip():
    registry = make_registry()
    index = BsimIndex(registry)
    index.add(bsim_sig("a", LINES_A, calls=1), sidecar("good", "liba-1"))
    restored = BsimIndex.from_json(index.to_json(), registry)
    target = bsim_sig("target", LINES_A, calls=1)
    assert [c.key for c in restored.query(target).candidates] == ["good"]


# ---------------------------------------------------------------------------- gating (B-06, P10.5)

def test_no_corpus_or_expensive_cold_index_stays_disabled():  # B-06
    config = RetrievalConfig(function_id_retrieval=True, bsim_retrieval=True)
    empty = Registry()
    admission = admit_retrieval(config, empty, bsim=BsimIndex(empty))
    assert not admission.allowed
    assert any("registry" in reason for reason in admission.reasons)
    lane = FidLane(FunctionIdIndex(empty), empty, config)
    assert lane.lookup("sig", "mips", "").status == "denied"
    lane = BsimLane(BsimIndex(empty), empty, config)
    assert lane.query(bsim_sig("target", LINES_A)).status == "denied"
    # a usable registry but an empty index still refuses BSim
    registry = make_registry()
    admission = admit_retrieval(config, registry, bsim=BsimIndex(registry))
    assert not admission.allowed
    assert any("no signature" in reason for reason in admission.reasons)
    # an expensive cold index build stays disabled under the configured cap
    index = BsimIndex(registry, cold=True, build_cost=120.0)
    index.add(bsim_sig("a", LINES_A, calls=1), sidecar("good", "liba-1"))
    capped = replace(config, bsim_max_cold_cost=60.0)
    admission = admit_retrieval(capped, registry, bsim=index)
    assert not admission.allowed
    assert any("cold BSim index build cost" in reason for reason in admission.reasons)
    assert admit_retrieval(replace(config, bsim_max_cold_cost=200.0), registry, bsim=index).ok()


def test_default_disabled_and_route_falls_through_to_similarity():
    config = RetrievalConfig()
    assert config.function_id_retrieval is False and config.bsim_retrieval is False
    assert config.external_queries is False
    registry = make_registry()
    index = BsimIndex(registry)
    admission = admit_retrieval(config, registry, bsim=index)
    assert admission.ok() and "disabled by default" in admission.reasons[0]
    plan = route_plan(config, registry, bsim=index)
    assert [step.name for step in plan] == list(DEFAULT_ORDER)
    assert enabled_routes(plan) == ["exact_twins", "lightweight_similarity"]
    by_name = {step.name: step for step in plan}
    assert by_name["function_id"].enabled is False
    assert "function_id_retrieval is disabled (default off)" == by_name["function_id"].reason
    assert by_name["bsim"].enabled is False
    assert "bsim_retrieval is disabled (default off)" == by_name["bsim"].reason
    # the lanes refuse without touching the indexes: the route falls through to similarity alone
    fid_index = FunctionIdIndex(registry)
    bsim_index = BsimIndex(registry)
    result = FidLane(fid_index, registry, config).lookup("sig", "mips", "")
    assert result.status == "disabled" and fid_index.lookups == 0
    result = BsimLane(bsim_index, registry, config).query(bsim_sig("target", LINES_A))
    assert result.status == "disabled" and bsim_index.queries == 0


def test_external_queries_denied_by_default(monkeypatch):  # P10.5
    registry = make_registry()
    config = RetrievalConfig(function_id_retrieval=True, bsim_retrieval=True)
    fid_lane = FidLane(FunctionIdIndex(registry), registry, config)
    bsim_lane = BsimLane(BsimIndex(registry), registry, config)
    fake_toolchain(monkeypatch, fid_module)
    fake_toolchain(monkeypatch, bsim_module)
    assert fid_lane.lookup("sig", "mips", "", external=True).status == "denied"
    assert "no external Function ID queries by default" in \
        fid_lane.lookup("sig", "mips", "", external=True).reason
    assert bsim_lane.query(bsim_sig("target", LINES_A), external=True).status == "denied"
    # the explicit opt-in enables them; local (non-external) queries never needed it
    opted_in = replace(config, external_queries=True)
    assert FidLane(FunctionIdIndex(registry), registry, opted_in) \
        .lookup("sig", "mips", "", external=True).status == "empty"
    index = BsimIndex(registry)
    index.add(bsim_sig("a", LINES_A, calls=1), sidecar("good", "liba-1"))
    assert BsimLane(index, registry, opted_in) \
        .query(bsim_sig("target", LINES_A, calls=1), external=True).status == "ok"


def test_route_plan_configurable_order():  # P10.4
    config = RetrievalConfig(route_order=("lightweight_similarity", "exact_twins", "bsim",
                                          "function_id"))
    plan = route_plan(config, make_registry())
    assert tuple(step.name for step in plan) == ("lightweight_similarity", "exact_twins", "bsim",
                                                 "function_id")
    with pytest.raises(ValueError):
        RetrievalConfig(route_order=("bsim", "bsim"))
    with pytest.raises(ValueError):
        RetrievalConfig(route_order=("remote_index",))
    # with flags on and an admitted corpus, the FID/BSim steps enable in the configured order
    registry = make_registry()
    index = BsimIndex(registry)
    index.add(bsim_sig("a", LINES_A, calls=1), sidecar("good", "liba-1"))
    enabled = replace(config, function_id_retrieval=True, bsim_retrieval=True)
    plan = route_plan(enabled, registry, bsim=index)
    assert tuple(step.name for step in plan) == ("lightweight_similarity", "exact_twins", "bsim",
                                                 "function_id")
    assert all(step.enabled for step in plan)


# ---------------------------------------------------------------------------- real-lane gate

def test_lane_returns_explicit_skip_when_toolchain_missing(monkeypatch):
    # host-independent: when analyzeHeadless/java are unavailable the lanes skip with the given
    # reason and never touch the index
    registry = make_registry()
    config = RetrievalConfig(function_id_retrieval=True, bsim_retrieval=True)
    fake_toolchain(monkeypatch, fid_module, ok=False, reason="no analyzeHeadless/java (explicit)")
    fake_toolchain(monkeypatch, bsim_module, ok=False, reason="no analyzeHeadless/java (explicit)")
    fid_index = FunctionIdIndex(registry)
    result = FidLane(fid_index, registry, config).lookup("sig", "mips", "")
    assert result.status == "skipped" and "explicit" in result.reason
    assert fid_index.lookups == 0
    bsim_index = BsimIndex(registry)
    bsim_index.add(bsim_sig("a", LINES_A, calls=1), sidecar("good", "liba-1"))
    result = BsimLane(bsim_index, registry, config).query(bsim_sig("target", LINES_A))
    assert result.status == "skipped" and "explicit" in result.reason
    assert bsim_index.queries == 0


def test_real_lane_skips_without_analyzeheadless_or_java():
    ok, reason = toolchain_available()
    if not ok:
        pytest.skip(reason)  # the explicit reason for skipping the real FID/BSim lanes
    assert ok
    lane = FidLane(FunctionIdIndex(make_registry()), make_registry(),
                   RetrievalConfig(function_id_retrieval=True))
    assert lane.available()[0] is True
