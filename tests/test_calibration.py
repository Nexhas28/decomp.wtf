"""Compiler profiles and bounded calibration (plan P4.1; tests D-07 and the V-05 groundwork).

No compiler needed: profile identities are content digests over the registry's pinned
specification and any recorded installation manifest (.decompnet-toolchain.json), and
calibration is driven by a stubbed judge -- the API under test never runs a toolchain.
"""

from __future__ import annotations

import json

import pytest

from decompnet import snapshots, toolchains
from decompnet.network import Network

from .test_permute import make_project  # shared synthetic-project fixture


@pytest.fixture
def net(tmp_path):
    n = Network(tmp_path / "cal.db")
    make_project(n, 3)
    return n


def make_profile(**kw) -> toolchains.Profile:
    base = dict(toolchain="gcc-2.7.2-kmc", wrapper="cc", flags=["-O2"], language="c",
                processor_mode="", abi=[], scope="", provenance="guess", timeout=20.0)
    base.update(kw)
    return toolchains.Profile(**base)


def write_manifest(tc_root, tid, sha):
    """A recorded installation manifest with different pinned binaries (no actual install).

    The folder is where `toolchains.path` resolves the id: a Linux-only toolchain lives under `linux/`
    (it runs in the compiler container), so writing to a flat path would leave the identity computation
    reading a manifest that isn't there. "wsl" is the manifest key older installs wrote."""
    d = toolchains.path(tid)  # exactly where the identity computation reads the manifest
    d.mkdir(parents=True, exist_ok=True)
    (d / toolchains.MANIFEST).write_text(json.dumps({
        "id": tid, "parts": [{"url": "https://example.invalid/x.tar.gz", "sha256": sha}],
        "commit": None, "wsl": False, "installed_at": 1.0,
    }))


def set_compiler(net: Network, compiler: dict) -> None:
    with net.store.tx() as db:
        db.execute("UPDATE projects SET config=? WHERE id=1",
                   (json.dumps({"kind": "rom", "compiler": compiler}),))


# ------------------------------------------------------------------------------
# Profile identity: every component of the profile participates (P4.1.1)
# ------------------------------------------------------------------------------

def test_flag_change_changes_profile_identity():
    assert make_profile(flags=["-O2"]).identity() != make_profile(flags=["-O0"]).identity()


def test_wrapper_change_changes_profile_identity():
    assert make_profile(wrapper="cc").identity() != make_profile(wrapper="cmd").identity()


def test_language_dialect_change_changes_profile_identity():
    assert make_profile(language="c").identity() != make_profile(language="c++").identity()


@pytest.mark.parametrize("field,value", [
    ("processor_mode", "thumb"),
    ("abi", [["gp", "0"]]),
    ("scope", "src/game.c"),
    ("timeout", 60.0),
])
def test_remaining_profile_components_participate_in_identity(field, value):
    assert make_profile().identity() != make_profile(**{field: value}).identity()


def test_ordered_flags_are_order_sensitive():
    assert make_profile(flags=["-O2", "-G0"]).identity() != make_profile(flags=["-G0", "-O2"]).identity()


# ------------------------------------------------------------------------------
# Toolchain binaries participate through their recorded install manifests (P4.1.1)
# ------------------------------------------------------------------------------

def test_reinstalled_toolchain_changes_profile_identity(tmp_path, monkeypatch):
    """Installing the toolchain, and reinstalling it with different pinned binaries, both
    change the profile identity; an identical reinstall does not."""
    monkeypatch.setenv("DECOMPNET_TOOLCHAINS", str(tmp_path / "toolchains"))
    p = make_profile()
    before = p.identity()
    write_manifest(tmp_path, p.toolchain, "1" * 64)
    first_install = p.identity()
    assert first_install != before                       # not installed -> installed
    write_manifest(tmp_path, p.toolchain, "2" * 64)
    second = p.identity()
    assert second != first_install                  # reinstalled with other binaries
    # an identical reinstall (same pins, later timestamp) keeps the same identity
    d = tmp_path / "toolchains" / p.toolchain
    m = json.loads((d / toolchains.MANIFEST).read_text())
    m["installed_at"] = 2.0
    (d / toolchains.MANIFEST).write_text(json.dumps(m))
    assert p.identity() == second and p.identity() != before


def test_identity_covers_required_toolchains(tmp_path, monkeypatch):
    """ee-gcc-2.96 requires runtime-i386: reinstalling the runtime changes the identity."""
    monkeypatch.setenv("DECOMPNET_TOOLCHAINS", str(tmp_path / "toolchains"))
    p = make_profile(toolchain="ee-gcc-2.96")
    before = p.identity()
    write_manifest(tmp_path, "runtime-i386", "3" * 64)
    assert p.identity() != before


def test_unknown_or_empty_toolchains_still_have_distinct_identities():
    empty = toolchains.Profile("").identity()
    assert toolchains.Profile("not-a-toolchain").identity() != empty
    assert toolchains.Profile("gcc-2.7.2-kmc").identity() != empty


# ------------------------------------------------------------------------------
# Provenance labels (P4.1.2): hypotheses stay hypotheses
# ------------------------------------------------------------------------------

@pytest.mark.parametrize("label", ["guess", "explicit", "calibrated"])
def test_provenance_labels_survive_round_trip(label):
    p = make_profile(provenance=label)
    q = toolchains.Profile.from_dict(p.to_dict())
    assert q == p
    assert q.provenance == label
    assert q.identity() == p.identity()


def test_unknown_provenance_in_a_config_is_coerced_to_guess():
    p = toolchains.profile_from({"toolchain": "gcc-2.7.2-kmc", "provenance": "future-label"})
    assert p.provenance == toolchains.PROVENANCE_GUESS


def test_provenance_is_metadata_not_compilation_identity():
    """A guess that is later confirmed compiles identically: provenance alone must not
    invalidate compilation/draft/score caches (documented contract)."""
    assert make_profile(provenance="guess").identity() == make_profile(provenance="calibrated").identity()


# ------------------------------------------------------------------------------
# profile_of / profile_identity accept every caller shape (P4.1.8)
# ------------------------------------------------------------------------------

def test_profile_of_accepts_profile_config_and_project(net):
    p = make_profile()
    assert toolchains.profile_of(p) is p
    assert toolchains.profile_of(p.to_dict()) == p
    assert toolchains.profile_of({"compiler": p.to_dict()}) == p


def test_profile_identity_accepts_project_rows_and_configs(net):
    set_compiler(net, {"toolchain": "gcc-2.7.2-kmc", "flags": ["-O2"], "timeout": 20})
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    direct = toolchains.profile_identity({"toolchain": "gcc-2.7.2-kmc", "flags": ["-O2"], "timeout": 20})
    assert toolchains.profile_identity(proj) == direct


def test_profile_identity_of_an_empty_config_is_stable():
    assert toolchains.profile_identity({}) == toolchains.Profile("").identity()
    assert toolchains.profile_identity({"config": "{}"}) == toolchains.Profile("").identity()


def test_profile_identity_accepts_an_already_parsed_project_row():
    parsed = {"config": {"kind": "rom", "compiler": {"toolchain": "gcc-2.7.2-kmc", "flags": ["-O2"]}}}
    assert toolchains.profile_identity(parsed) == toolchains.profile_identity(
        {"toolchain": "gcc-2.7.2-kmc", "flags": ["-O2"]})


def test_legacy_compiler_dict_mutations_are_still_seen():
    """Drop-in compatibility for snapshots.compiler_digest, which hashes the whole compiler
    dict: every mutation that digest sees, the profile identity sees too."""
    base = {"toolchain": "gcc-2.7.2-kmc", "flags": ["-O2"], "timeout": 20}
    for mutation in ({"toolchain": "gcc-2.8.1-psx"}, {"flags": ["-O0"]}, {"timeout": 30}):
        assert toolchains.profile_identity({**base, **mutation}) != toolchains.profile_identity(base)


# ------------------------------------------------------------------------------
# Versioned profile changes: changelog helper and the invalidation contract (P4.1.8)
# ------------------------------------------------------------------------------

def test_profile_diff_reports_changed_fields_and_invalidation():
    a = make_profile(flags=["-O2"])
    b = make_profile(flags=["-O0"], provenance="calibrated")
    change = toolchains.profile_diff(a, b)
    assert change.changed == ("flags",)
    assert change.before_identity == a.identity() and change.after_identity == b.identity()
    assert change.provenance == ("guess", "calibrated")  # recorded, even though not part of identity
    assert change.is_identity_change
    assert set(change.invalidates) == {"compilation", "drafts", "scores", "search_budgets"}
    assert change.as_dict()["changed"] == ["flags"]


def test_profile_diff_without_identity_change_invalidates_nothing():
    a = make_profile()
    b = make_profile(provenance="explicit")
    change = toolchains.profile_diff(a, b)
    assert change.changed == ()
    assert not change.is_identity_change
    assert change.invalidates == ()


# ------------------------------------------------------------------------------
# Bounded calibration (P4.1.4): bounded, per-profile results, never mutates config
# ------------------------------------------------------------------------------

def test_d07_inconclusive_calibration_never_replaces_the_project_config():
    """D-07: plausible-but-wrong profiles leave the calibration inconclusive, and the
    caller's project configuration is never touched (calibrate returns a recommendation
    record; the engine decides whether to commit anything)."""
    config = {"kind": "rom", "compiler": {"toolchain": "gcc-2.7.2-kmc", "flags": ["-O2"], "timeout": 20}}
    before = json.dumps(config, sort_keys=True)
    candidates = [make_profile(flags=["-O0"]), make_profile(toolchain="gcc-2.8.1-psx")]
    sample = [("asm-a", "data-a", "header-a"), ("asm-b", "data-b", "header-b")]

    def stub(profile, asm, data, header):
        # plausible hypotheses the judge rejects or cannot confirm
        return {"verdict": "wrong" if profile.flags == ("-O0",) else "inconclusive",
                "score": 0.0, "evidence": "target bytes disagree / cannot tell"}

    report = toolchains.calibrate(candidates, sample, stub)
    assert report.status == "inconclusive"
    assert report.recommendation is None and report.recommendation_identity is None
    assert [r.verdict for r in report.results] == ["wrong", "inconclusive"]
    assert json.dumps(config, sort_keys=True) == before  # pure: nothing was mutated


def test_poor_draft_alone_does_not_mark_a_profile_wrong():
    """P4.1.5: source uncertainty and compiler uncertainty stay separate. A judge that can
    only see a poor draft answers 'inconclusive' -- the profile is never marked wrong."""
    report = toolchains.calibrate(
        [make_profile()], [("asm", "data", "header")],
        lambda profile, asm, data, header: {"verdict": "inconclusive", "score": None,
                                            "evidence": "draft is poor: source may be wrong, compiler may be wrong"})
    assert report.results[0].verdict == "inconclusive"  # not "wrong"
    assert report.status == "inconclusive" and report.recommendation is None


def test_only_judge_output_marks_a_profile_wrong():
    """The judge's per-profile verdict is the only signal that marks a profile wrong; the
    API never infers it from drafts, flags or sample content."""
    def stub(profile, asm, data, header):
        return {"verdict": "wrong", "score": 0.0, "evidence": "judge verified the bytes disagree"}
    report = toolchains.calibrate([make_profile()], [("asm", "data", "header")], stub)
    assert report.results[0].verdict == "wrong" and report.status == "inconclusive"


def test_calibration_recommends_a_unique_matching_profile():
    def stub(profile, asm, data, header):
        return {"verdict": "match" if "-O2" in profile.flags else "wrong",
                "score": 1.0, "evidence": "verified against the sample"}
    candidates = [make_profile(flags=["-O0"]), make_profile(flags=["-O2"])]
    report = toolchains.calibrate(candidates, [("asm", "data", "header")], stub)
    assert report.status == "recommended"
    assert report.recommendation == candidates[1]
    assert report.recommendation_identity == candidates[1].identity()
    assert [r.verdict for r in report.results] == ["wrong", "match"]


def test_multiple_matching_profiles_stay_ambiguous():
    """A coherent sample that cannot separate two hypotheses recommends neither: the caller
    decides (P4.1: require a bounded hypothesis set, expose inconclusive results)."""
    def stub(profile, asm, data, header):
        return {"verdict": "match", "score": 1.0, "evidence": "ok"}
    report = toolchains.calibrate([make_profile(), make_profile(flags=["-O0"])],
                                  [("asm", "data", "header")], stub)
    assert report.status == "ambiguous" and report.recommendation is None


def test_calibration_is_bounded_and_reports_partial_results():
    """The budget (max_profiles x max_samples judge calls) is respected; candidates beyond
    it are reported as skipped, never silently evaluated."""
    calls = []
    def stub(profile, asm, data, header):
        calls.append((profile.toolchain, tuple(profile.flags), asm))
        return {"verdict": "match", "score": 1.0, "evidence": "ok"}
    candidates = [make_profile(flags=[f"-O{i}"]) for i in range(4)]
    sample = [("a", "d", "h"), ("b", "d", "h"), ("c", "d", "h")]
    report = toolchains.calibrate(candidates, sample, stub, max_profiles=2, max_samples=2)
    assert report.judge_calls == 4                       # 2 profiles x 2 samples, no more
    assert len(report.results) == 2 and len(report.results[0].samples) == 2
    assert list(report.skipped) == candidates[2:]
    assert report.max_profiles == 2 and report.max_samples == 2


def test_calibration_stops_a_profile_after_an_explicit_wrong():
    """A profile the judge explicitly rejected does not consume the rest of the sample."""
    calls = []
    def stub(profile, asm, data, header):
        calls.append(asm)
        return {"verdict": "wrong" if asm == "a" else "match", "score": 0.0, "evidence": "x"}
    report = toolchains.calibrate([make_profile()], [("a", "d", "h"), ("b", "d", "h"), ("c", "d", "h")], stub)
    assert len(calls) == 1 and report.judge_calls == 1
    assert report.results[0].verdict == "wrong" and report.results[0].truncated


def test_judge_exceptions_are_inconclusive_not_fatal():
    def stub(profile, asm, data, header):
        raise RuntimeError("judge crashed")
    report = toolchains.calibrate([make_profile()], [("a", "d", "h")], stub)
    assert report.results[0].verdict == "inconclusive"
    assert "judge crashed" in report.results[0].error


def test_bool_and_bare_score_judge_results_are_normalised():
    assert toolchains.calibrate([make_profile()], [("a", "d", "h")], lambda p, a, d, h: True).results[0].verdict == "match"
    assert toolchains.calibrate([make_profile()], [("a", "d", "h")], lambda p, a, d, h: False).results[0].verdict == "wrong"
    # a bare score is a score, not a verdict: it cannot declare a match or a wrong profile
    r = toolchains.calibrate([make_profile()], [("a", "d", "h")], lambda p, a, d, h: 0.97)
    assert r.results[0].verdict == "inconclusive" and r.results[0].score == 0.97


def test_sample_items_reach_the_judge_verbatim():
    seen = []
    def stub(profile, asm, data, header):
        seen.append((asm, data, header))
        return {"verdict": "match", "score": 1.0, "evidence": ""}
    sample = [("asm-text", b"\x01\x02", "int x;")]
    toolchains.calibrate([make_profile()], sample, stub)
    assert seen == sample


# ------------------------------------------------------------------------------
# V-05 groundwork: profile identity change between capture and acceptance
# ------------------------------------------------------------------------------

def test_v05_profile_change_between_capture_and_acceptance(net, tmp_path, monkeypatch):
    """V-05: an old-profile score is not treated as a current match. The acceptance gate
    (snapshots.check_current compares compiler_digest) must observe every profile identity
    change; PR-14 makes toolchains.profile_identity the provider behind that digest."""
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    set_compiler(net, {"toolchain": "gcc-2.7.2-kmc", "flags": ["-O2"], "timeout": 20})
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    identity_at_capture = toolchains.profile_identity(proj)
    snap = snapshots.capture(fn, proj, net._context(fn["project_id"], fn["unit"]))
    # the gate as it exists today passes at capture time
    assert snapshots.compiler_digest(proj) == snap["compiler_digest"]
    assert identity_at_capture == toolchains.profile_identity(proj)

    # between capture and acceptance, a flag change: the digest contract sees it
    set_compiler(net, {"toolchain": "gcc-2.7.2-kmc", "flags": ["-O0"], "timeout": 20})
    proj_flags = net.store.one("SELECT * FROM projects WHERE id=1")
    assert snapshots.compiler_digest(proj_flags) != snap["compiler_digest"]
    assert toolchains.profile_identity(proj_flags) != identity_at_capture

    # between capture and acceptance, a wrapper change (not expressible in the legacy
    # compiler dict alone): the profile identity is the observable that detects it
    set_compiler(net, {"toolchain": "gcc-2.7.2-kmc", "wrapper": "cmd", "flags": ["-O2"], "timeout": 20})
    proj_wrapper = net.store.one("SELECT * FROM projects WHERE id=1")
    assert toolchains.profile_identity(proj_wrapper) != identity_at_capture

    # between capture and acceptance, the toolchain is reinstalled with different pinned
    # binaries: only the recorded install manifest participates, and it changes the identity
    monkeypatch.setenv("DECOMPNET_TOOLCHAINS", str(tmp_path / "toolchains"))
    write_manifest(tmp_path, "gcc-2.7.2-kmc", "9" * 64)
    assert toolchains.profile_identity(proj) != identity_at_capture


def test_identity_and_legacy_digest_agree_on_config_visible_changes(net):
    """Compatibility: wherever snapshots.compiler_digest already sees a change, the profile
    identity sees it too -- so wiring the identity into the digest cannot miss anything the
    current contract detects."""
    set_compiler(net, {"toolchain": "gcc-2.7.2-kmc", "flags": ["-O2"], "timeout": 20})
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    base_digest, base_identity = snapshots.compiler_digest(proj), toolchains.profile_identity(proj)
    for compiler in ({"toolchain": "gcc-2.8.1-psx", "flags": ["-O2"], "timeout": 20},
                     {"toolchain": "gcc-2.7.2-kmc", "flags": ["-O0"], "timeout": 20},
                     {"toolchain": "gcc-2.7.2-kmc", "flags": ["-O2"], "timeout": 60}):
        set_compiler(net, compiler)
        changed = net.store.one("SELECT * FROM projects WHERE id=1")
        assert snapshots.compiler_digest(changed) != base_digest
        assert toolchains.profile_identity(changed) != base_identity
