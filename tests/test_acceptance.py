"""Version-safe acceptance and remote-result trust (plan P1.4, P1.5: V-01..V-08, X-01).

Stub-based: no compiler or toolchain needed. Interleavings are forced by monkeypatching a compile /
regression hook to perform the concurrent mutation exactly once (a flag), never by sleeping.

Covered here:
- V-01 stale-context attempt is recorded historical, never accepted
- V-02 a match accepted during edit_context validation forces a re-test before commit
- V-03 two header edits race: only a validated current-version edit commits
- V-04 target/reloc change before acceptance -> stale
- V-05 compiler-profile change -> stale
- V-06 function revision bump (split/reset) -> stale
- V-07 per-unit header edit re-tests only that unit's matched set (fork stub)
- V-08 an unrelated unit's match does not conflict a per-unit edit (scoped epochs)
- X-01 forged / genuine remote results (mocked collab ingress) are re-verified locally
"""

from __future__ import annotations

import json

import pytest

from decompnet import snapshots
from decompnet.app.engine import RomNetwork
from decompnet.network import Network

FP = "9f86d081884c7d659a2feaa0c55cd015a3bf4f1b2b0b822cd15d6c15b0f00a08"  # fixture fingerprint


def make_project(n: Network, n_functions: int = 1, unit: str = "", fingerprint: str = FP):
    """A synthetic hash-only project (target is a one-way fingerprint), like the real coordinators'."""
    with n.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'fixture', 'fixture', 'acceptance fixture', 'mips', 0, '/tmp/fixture', ?)",
                   (json.dumps({"compiler": {"toolchain": "gcc-mips", "flags": ["-O2"]}}),))
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, 'int f(void);', NULL, 'fixture', 0, ?)", (unit,))
        for i in range(1, n_functions + 1):
            db.execute(
                "INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, status, "
                "fingerprint, unit, vram) VALUES(?,1,?,8,1,NULL,'[]','','open',?,?,?)",
                (i, f"f{i}", fingerprint, unit, 0x80000000 + i))
    return n.store.one("SELECT * FROM projects WHERE id=1")


def add_unit_context(n: Network, unit: str, version: int, header: str = "int f(void);") -> None:
    with n.store.tx() as db:
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, ?, ?, NULL, 'm', 0, ?)", (version, header, unit))


def ctx(net, fn):
    return net._context(fn["project_id"], fn["unit"])


def snapshot_of(net, fn):
    proj = net.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
    return snapshots.capture(fn, proj, ctx(net, fn))


def match_fn(net, agent, fn, source: str) -> None:
    """Record a match the slow way (same path attempt() uses when everything is in order)."""
    net._record(fn, agent, 0, source, ctx(net, fn)["version"], matched=True, compiled=True,
                snapshot=snapshot_of(net, fn))


class FakeResult:
    """A compile that always succeeds."""
    ok = True
    log = ""
    obj = b"\x03\xe0\x00\x08"


class FakeCand:
    """An evaluated candidate whose fingerprint matches the fixture target."""
    @staticmethod
    def fingerprint() -> str:
        return FP


class ForgedCand:
    """A candidate that does not match the fixture target."""
    @staticmethod
    def fingerprint() -> str:
        return "forged"


def stub_compile_eval(net, monkeypatch, cand=None, hook=None) -> None:
    """net._compile succeeds (running `hook` first, once per compile call) and net._evaluate
    returns the given candidate (a fingerprint match by default)."""
    def compile_(proj, header, source, fn=None):
        if hook:
            hook()
        return FakeResult()
    monkeypatch.setattr(net, "_compile", compile_)
    monkeypatch.setattr(net, "_evaluate", lambda fn, proj, obj: (cand if cand is not None else FakeCand(), None, []))


@pytest.fixture
def net(tmp_path):
    n = Network(tmp_path / "acceptance.db")
    make_project(n, 1)
    return n


@pytest.fixture
def alice(net):
    return net.authenticate(net.register_agent("alice"))


# ----------------------------------------------------------------------------- V-01..V-08

def test_v01_header_change_between_compile_and_acceptance_is_historical(net, alice, monkeypatch):
    """The header moves to v2 while the agent's source compiles: the old-context result is recorded
    as historical search evidence and is never accepted as a current match (plan V-01)."""
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    fired = {"n": 0}

    def compile_under_race(proj, header, source, fn=None):
        fired["n"] += 1
        if fired["n"] == 1:  # the concurrent header edit, exactly once
            with net.store.tx() as db:
                db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                           "VALUES(1,2,'int f(void); int changed;',NULL,'m',0,'')")
        return FakeResult()

    monkeypatch.setattr(net, "_compile", compile_under_race)
    monkeypatch.setattr(net, "_evaluate", lambda fn, proj, obj: (FakeCand(), None, []))
    out = net.attempt(alice, "f1", "int f(void) { return 1; }")
    assert out["matched"] is False
    assert out["score"] == 0  # search evidence is still returned...
    assert "context changed during your compile (v1 -> v2)" in out["text"]
    assert "not counted" in out["text"]
    fresh = net.store.one("SELECT * FROM functions WHERE id=1")
    assert fresh["status"] == "open"  # ...but never accepted
    assert fresh["best_score"] is None and fresh["matched_source"] is None
    snap = net.store.one("SELECT stale_reason FROM attempt_snapshots WHERE function_id=1")
    assert snap["stale_reason"] == "context changed"
    assert net.store.one("SELECT * FROM verification_receipts WHERE function_id=1") is None
    assert net.store.one("SELECT matched_epoch FROM projects WHERE id=1")["matched_epoch"] == 0


def test_v01b_scored_path_stale_compile_is_historical_too(net, alice, monkeypatch):
    """The same V-01 invariant on the scored (non-hash-only) path: the score is returned as search
    evidence, the diff verdict is withheld, and the stale result never becomes best or matched."""
    from decompnet.elfobj import FunctionObj

    with net.store.tx() as db:
        db.execute("UPDATE functions SET target=?, fingerprint=NULL WHERE id=1", (b"\x03\xe0\x00\x08",))
    fired = {"n": 0}

    def compile_under_race(proj, header, source, fn=None):
        fired["n"] += 1
        if fired["n"] == 1:
            with net.store.tx() as db:
                db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                           "VALUES(1,2,'int f(void); int changed;',NULL,'m',0,'')")
        return FakeResult()

    cand = FunctionObj("f1", "mips", False, b"\x03\xe0\x00\x08", [])
    monkeypatch.setattr(net, "_compile", compile_under_race)
    monkeypatch.setattr(net, "_evaluate", lambda fn, proj, obj: (cand, None, []))
    out = net.attempt(alice, "f1", "int f(void) { return 1; }")
    assert out["matched"] is False and out["score"] == 0  # evidence returned, never accepted
    assert "context changed during your compile (v1 -> v2)" in out["text"]
    assert "✅ MATCH" not in out["text"] and "not counted" in out["text"]
    fresh = net.store.one("SELECT * FROM functions WHERE id=1")
    assert fresh["status"] == "open" and fresh["best_score"] is None and fresh["matched_source"] is None
    assert net.store.one("SELECT stale_reason FROM attempt_snapshots WHERE function_id=1")["stale_reason"] \
        == "context changed"
    assert net.store.one("SELECT * FROM verification_receipts WHERE function_id=1") is None


def test_v02_match_during_edit_validation_forces_a_retest(net, alice, monkeypatch):
    """A new function matches while a header edit validates: the matched-set epoch forces a re-test
    against the new matched set before the edit commits (plan V-02)."""
    fn2 = net.store.one("SELECT * FROM functions WHERE id=1")
    calls = {"n": 0}
    real_regression = net.regression_test

    def racing_regression(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            fresh = net.store.one("SELECT * FROM functions WHERE id=1")
            match_fn(net, alice, fresh, "int f(void) { return 1; }")
        return real_regression(*args, **kwargs)

    monkeypatch.setattr(net, "regression_test", racing_regression)
    stub_compile_eval(net, monkeypatch)
    out = net.edit_context(alice, "fixture", "", "int extra;\n", "add a prototype")
    assert "now v2" in out
    assert calls["n"] == 2  # the first validation was voided by the match; the edit re-tested
    matched = net.store.one("SELECT * FROM functions WHERE id=1")
    assert matched["status"] == "matched"
    assert matched["matched_source"].strip().endswith("return 1; }")


def test_v03_two_header_edits_race_only_a_current_version_commit_wins(net, alice, monkeypatch):
    """Someone else commits header v2 while this edit validates: this edit must re-apply on top and
    re-test before committing its own version (plan V-03; the version guard predates this PR and
    this test pins it)."""
    calls = {"n": 0}
    real_regression = net.regression_test

    def racing_regression(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            with net.store.tx() as db:
                version = net._next_version(db, 1)
                db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                           "VALUES(1,?,?,NULL,'the other edit',0,'')",
                           (version, "int f(void); int other;\n"))
        return real_regression(*args, **kwargs)

    monkeypatch.setattr(net, "regression_test", racing_regression)
    stub_compile_eval(net, monkeypatch)
    out = net.edit_context(alice, "fixture", "", "int mine;\n", "add mine")
    assert "now v3" in out
    assert calls["n"] == 2
    header = net.get_context("fixture")
    assert "int other;" in header and "int mine;" in header  # both edits landed, in order


def test_v04_reloc_change_before_acceptance_is_rejected(net, alice, monkeypatch):
    """The relocation layout changes while a worker runs: the stale linking verdict cannot be
    accepted (plan V-04)."""
    fired = {"n": 0}

    def compile_under_race(proj, header, source, fn=None):
        fired["n"] += 1
        if fired["n"] == 1:
            with net.store.tx() as db:
                db.execute("UPDATE functions SET relocs='[[0,4,\"sym\",0]]' WHERE id=1")
        return FakeResult()

    monkeypatch.setattr(net, "_compile", compile_under_race)
    monkeypatch.setattr(net, "_evaluate", lambda fn, proj, obj: (FakeCand(), None, []))
    out = net.attempt(alice, "f1", "int f(void) { return 1; }")
    assert "target changed" in out["text"] and "not counted" in out["text"]
    assert out["matched"] is False
    fresh = net.store.one("SELECT * FROM functions WHERE id=1")
    assert fresh["status"] == "open" and fresh["matched_source"] is None
    assert net.store.one("SELECT stale_reason FROM attempt_snapshots WHERE function_id=1")["stale_reason"] \
        == "target changed"


def test_v05_compiler_profile_change_is_stale(net, alice, monkeypatch):
    """The compiler profile changes while a worker runs: the old-profile score is not a current
    match (plan V-05)."""
    fired = {"n": 0}

    def compile_under_race(proj, header, source, fn=None):
        fired["n"] += 1
        if fired["n"] == 1:
            with net.store.tx() as db:
                db.execute("UPDATE projects SET config=? WHERE id=1",
                           (json.dumps({"compiler": {"toolchain": "gcc-mips", "flags": ["-O0"]}}),))
        return FakeResult()

    monkeypatch.setattr(net, "_compile", compile_under_race)
    monkeypatch.setattr(net, "_evaluate", lambda fn, proj, obj: (FakeCand(), None, []))
    out = net.attempt(alice, "f1", "int f(void) { return 1; }")
    assert "compiler profile changed" in out["text"] and "not counted" in out["text"]
    assert out["matched"] is False
    fresh = net.store.one("SELECT * FROM functions WHERE id=1")
    assert fresh["status"] == "open"
    assert net.store.one("SELECT stale_reason FROM attempt_snapshots WHERE function_id=1")["stale_reason"] \
        == "compiler profile changed"


def test_v06_revision_bump_makes_evidence_historical(net, alice, monkeypatch):
    """The function is split/reset (revision bump) during analysis: the evidence stays attached to
    the old identity until revalidated (plan V-06)."""
    fired = {"n": 0}

    def compile_under_race(proj, header, source, fn=None):
        fired["n"] += 1
        if fired["n"] == 1:
            with net.store.tx() as db:
                db.execute("UPDATE functions SET revision=revision+1 WHERE id=1")
        return FakeResult()

    monkeypatch.setattr(net, "_compile", compile_under_race)
    monkeypatch.setattr(net, "_evaluate", lambda fn, proj, obj: (FakeCand(), None, []))
    out = net.attempt(alice, "f1", "int f(void) { return 1; }")
    assert "split or reset" in out["text"] and "not counted" in out["text"]
    assert out["matched"] is False
    fresh = net.store.one("SELECT * FROM functions WHERE id=1")
    assert fresh["status"] == "open"
    assert net.store.one("SELECT stale_reason FROM attempt_snapshots WHERE function_id=1")["stale_reason"] \
        == "function was split or reset"


def test_v07_unit_edit_re_tests_only_its_own_protected_matches(net, alice, monkeypatch):
    """Editing unit a.c's header re-tests only a.c's matched functions; b.c's header and matched
    source stay untouched (plan V-07, fork header-edit stub on per-unit contexts)."""
    with net.store.tx() as db:
        db.execute("UPDATE functions SET unit='a.c' WHERE id=1")
        db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, status, "
                   "fingerprint, unit, vram) VALUES(2,1,'f2',8,1,NULL,'[]','','open',?, 'b.c', 0x80000002)", (FP,))
    add_unit_context(net, "a.c", 2, "int f(void);")  # versions are project-global; '' is v1
    add_unit_context(net, "b.c", 3, "int f(void);")
    fn1 = net.store.one("SELECT * FROM functions WHERE id=1")
    fn2 = net.store.one("SELECT * FROM functions WHERE id=2")
    match_fn(net, alice, fn1, "int f(void) { return 1; }")
    match_fn(net, alice, fn2, "int f(void) { return 2; }")
    compiled = []

    def compile_(proj, header, source, fn=None):
        compiled.append(source)
        return FakeResult()

    monkeypatch.setattr(net, "_compile", compile_)
    monkeypatch.setattr(net, "_evaluate", lambda fn, proj, obj: (FakeCand(), None, []))
    out = net.edit_context(alice, "fixture", "", "int extra;\n", "a.c gets a prototype", function="f1")
    assert "now v4" in out
    assert all("return 2" not in src for src in compiled)  # b.c's matched source was never recompiled
    assert net._context(1, "b.c")["version"] == 3  # b.c's header untouched
    assert net.store.one("SELECT status FROM functions WHERE id=2")["status"] == "matched"
    assert net.store.one("SELECT matched_source FROM functions WHERE id=2")["matched_source"].strip() \
        .endswith("return 2; }")


def test_v08_unrelated_unit_match_does_not_conflict_a_unit_edit(net, alice, monkeypatch):
    """b.c accepts a function while a.c's header edit validates: the scoped epoch lets the valid
    a.c edit commit on its first validation — no global false conflict (plan V-08)."""
    with net.store.tx() as db:
        db.execute("UPDATE functions SET unit='a.c' WHERE id=1")
        db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, status, "
                   "fingerprint, unit, vram) VALUES(2,1,'f2',8,1,NULL,'[]','','open',?, 'b.c', 0x80000002)", (FP,))
    add_unit_context(net, "a.c", 2)
    add_unit_context(net, "b.c", 3)
    match_fn(net, alice, net.store.one("SELECT * FROM functions WHERE id=1"), "int f(void) { return 1; }")
    calls = {"n": 0}
    real_regression = net.regression_test

    def racing_regression(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            fresh = net.store.one("SELECT * FROM functions WHERE id=2")
            match_fn(net, alice, fresh, "int f(void) { return 2; }")  # unrelated unit accepts
        return real_regression(*args, **kwargs)

    monkeypatch.setattr(net, "regression_test", racing_regression)
    stub_compile_eval(net, monkeypatch)
    out = net.edit_context(alice, "fixture", "", "int extra;\n", "a.c gets a prototype", function="f1")
    assert "now v4" in out
    assert calls["n"] == 1  # committed on the first validation: the b.c match was out of scope
    assert net.store.one("SELECT status FROM functions WHERE id=2")["status"] == "matched"
    assert net.store.one("SELECT matched_epoch FROM projects WHERE id=1")["matched_epoch"] == 2


# ----------------------------------------------------------------------------- X-01

@pytest.fixture
def rnet(tmp_path):
    """A local engine (the collab ingress lives on RomNetwork)."""
    n = RomNetwork(tmp_path / "room.db", tmp_path)
    make_project(n, 1)
    return n


def test_x01_forged_remote_match_is_rejected_never_accepted(rnet, monkeypatch):
    """The room's host reports f1 matched, but the source does not match locally: rejected and
    recorded as historical — no matched status, no receipt, no epoch bump (plan X-01)."""
    stub_compile_eval(rnet, monkeypatch, cand=ForgedCand())
    rnet.apply_remote_match("fixture", 0x80000001, "int f(void) { return 9; }", "mallory")
    fresh = rnet.store.one("SELECT * FROM functions WHERE id=1")
    assert fresh["status"] == "open" and fresh["matched_source"] is None
    assert rnet.store.one("SELECT * FROM verification_receipts WHERE function_id=1") is None
    assert rnet.store.one("SELECT matched_epoch FROM projects WHERE id=1")["matched_epoch"] == 0
    snap = rnet.store.one("SELECT * FROM attempt_snapshots WHERE function_id=1")
    assert snap["stale_reason"] == "remote match failed local verification"
    attempt = rnet.store.one("SELECT via FROM attempts WHERE function_id=1")
    assert attempt["via"] == "room"


def test_x01_genuine_remote_match_is_re_verified_locally(rnet, monkeypatch):
    """A reported match that really matches locally is accepted through the normal acceptance path:
    receipt written and matched_epoch bumped (plan X-01)."""
    stub_compile_eval(rnet, monkeypatch)
    rnet.apply_remote_match("fixture", 0x80000001, "int f(void) { return 1; }", "host")
    fresh = rnet.store.one("SELECT * FROM functions WHERE id=1")
    assert fresh["status"] == "matched" and fresh["matched_source"].strip().endswith("return 1; }")
    assert rnet.store.one("SELECT * FROM verification_receipts WHERE function_id=1") is not None
    assert rnet.store.one("SELECT matched_epoch FROM projects WHERE id=1")["matched_epoch"] == 1
    snap = rnet.store.one("SELECT stale_reason FROM attempt_snapshots WHERE function_id=1")
    assert snap["stale_reason"] is None


def test_x01_remote_best_score_is_never_taken_on_trust(rnet, monkeypatch):
    """A member reports a better score for a source that does not compile to a local match: the
    reported score is not recorded; the source is re-scored locally and the hint is rejected."""
    agent = rnet.authenticate(rnet.register_agent("member"))
    stub_compile_eval(rnet, monkeypatch, cand=ForgedCand())
    rnet.apply_reported_best("fixture", "f1", 5, "int f(void) { return 9; }", agent)
    fresh = rnet.store.one("SELECT * FROM functions WHERE id=1")
    assert fresh["best_score"] is None and fresh["best_source"] is None  # not 5: never a remote score alone
    assert fresh["status"] == "open"
    snap = rnet.store.one("SELECT stale_reason FROM attempt_snapshots WHERE function_id=1")
    assert snap["stale_reason"] == "room score not confirmed locally"


def test_x01_remote_best_that_really_matches_is_accepted_locally(rnet, monkeypatch):
    """The same ingress path accepts only what local verification confirms: a reported improvement
    that is a genuine local match is accepted as a match, with a receipt."""
    agent = rnet.authenticate(rnet.register_agent("member"))
    stub_compile_eval(rnet, monkeypatch)
    rnet.apply_reported_best("fixture", "f1", 1, "int f(void) { return 1; }", agent)
    fresh = rnet.store.one("SELECT * FROM functions WHERE id=1")
    assert fresh["status"] == "matched"
    assert rnet.store.one("SELECT * FROM verification_receipts WHERE function_id=1") is not None
    assert rnet.store.one("SELECT matched_epoch FROM projects WHERE id=1")["matched_epoch"] == 1
