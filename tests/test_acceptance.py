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
- A-01..A-03 _record itself enforces snapshot safety for every caller (stale inside the acceptance
  transaction, no snapshot on a ROM project); budget resets are material and capped; revalidation
  only restores trust for a reproduced best
"""

from __future__ import annotations

import json

import pytest

from decompnet import snapshots
from decompnet.app.engine import RomNetwork
from decompnet.network import MAX_BUDGET_RESETS, Network

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


# ----------------------------------------------------------------------------- _record contract

def test_record_rejects_a_snapshot_that_went_stale_before_the_acceptance_transaction(net, alice):
    """The caller's snapshot was current when it looked, but the header moved before _record's own
    transaction: _record re-checks inside the transaction and stores the attempt as historical."""
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    snap = snapshot_of(net, fn)
    with net.store.tx() as db:
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(1,2,'int f(void); int changed;',NULL,'m',0,'')")
    net._record(fn, alice, 0, "int f(void) { return 1; }", 1, matched=True, compiled=True, snapshot=snap)
    fresh = net.store.one("SELECT * FROM functions WHERE id=1")
    assert fresh["status"] == "open" and fresh["matched_source"] is None and fresh["best_score"] is None
    assert net.store.one("SELECT stale_reason FROM attempt_snapshots WHERE function_id=1")["stale_reason"] \
        == "context changed"
    assert net.store.one("SELECT * FROM verification_receipts WHERE function_id=1") is None
    assert net.store.one("SELECT matched_epoch FROM projects WHERE id=1")["matched_epoch"] == 0
    assert net.store.one("SELECT COUNT(*) AS n FROM attempts WHERE function_id=1")["n"] == 1  # kept as evidence


def test_record_refuses_a_match_without_a_snapshot_for_a_rom_project(net, alice):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    net._record(fn, alice, 0, "int f(void) { return 1; }", 1, matched=True, compiled=True)
    fresh = net.store.one("SELECT * FROM functions WHERE id=1")
    assert fresh["status"] == "open" and fresh["matched_source"] is None
    assert net.store.one("SELECT stale_reason FROM attempt_snapshots WHERE function_id=1")["stale_reason"] \
        == "no snapshot captured before compile"
    assert net.store.one("SELECT * FROM verification_receipts WHERE function_id=1") is None
    assert net.store.one("SELECT matched_epoch FROM projects WHERE id=1")["matched_epoch"] == 0


def test_record_accepts_a_snapshotless_match_in_a_fork_project(net, alice, monkeypatch):
    """A fork's build judges the match itself, so the snapshot rule applies to ROM projects only."""
    monkeypatch.setattr(net, "fork_slug", lambda project: project, raising=False)
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    net._record(fn, alice, 0, "int f(void) { return 1; }", 1, matched=True, compiled=True)
    assert net.store.one("SELECT status FROM functions WHERE id=1")["status"] == "matched"
    assert net.store.one("SELECT * FROM verification_receipts WHERE function_id=1") is not None


def _budget(net, turns_used: int = 2, lifetime_resets: int = 0) -> None:
    with net.store.tx() as db:
        db.execute("INSERT OR REPLACE INTO search_budgets(function_id, epoch, turns_used, max_turns, lifetime_resets) "
                   "VALUES(1, '', ?, 4, ?)", (turns_used, lifetime_resets))


def _budget_row(net):
    return net.store.one("SELECT turns_used, reset_reason, lifetime_resets FROM search_budgets WHERE function_id=1")


def test_small_improvement_does_not_reset_the_budget(net, alice):
    with net.store.tx() as db:
        db.execute("UPDATE functions SET best_score=1000, best_source='int f(void) { return 0; }' WHERE id=1")
    _budget(net)
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    net._record(fn, alice, 990, "int f(void) { return 2; }", 1, snapshot=snapshot_of(net, fn))  # only 1% better
    assert net.store.one("SELECT best_score FROM functions WHERE id=1")["best_score"] == 990  # still the new best
    row = _budget_row(net)
    assert row["turns_used"] == 2 and row["reset_reason"] is None and row["lifetime_resets"] == 0
    net._record(fn, alice, 900, "int f(void) { return 3; }", 1, snapshot=snapshot_of(net, fn))  # >5% better
    row = _budget_row(net)
    assert row["turns_used"] == 0 and row["reset_reason"] == "better-source" and row["lifetime_resets"] == 1


def test_budget_resets_are_capped_for_the_functions_lifetime(net, alice):
    with net.store.tx() as db:
        db.execute("UPDATE functions SET best_score=1000, best_source='int f(void) { return 0; }' WHERE id=1")
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    score = 1000
    for i in range(MAX_BUDGET_RESETS + 1):
        _budget(net, lifetime_resets=i)  # i resets already used
        score = int(score * 0.9)  # every step is a material improvement
        net._record(fn, alice, score, f"int f(void) {{ return {i + 10}; }}", 1, snapshot=snapshot_of(net, fn))
        row = _budget_row(net)
        if i < MAX_BUDGET_RESETS:
            assert row["turns_used"] == 0 and row["lifetime_resets"] == i + 1
        else:
            assert row["turns_used"] == 2 and row["lifetime_resets"] == MAX_BUDGET_RESETS  # cap reached: no reset
    assert net.store.one("SELECT best_score FROM functions WHERE id=1")["best_score"] == score  # still recorded


def test_revalidate_bests_keeps_an_unreproduced_best_unverified(net, monkeypatch):
    with net.store.tx() as db:
        db.execute("UPDATE functions SET best_score=5, best_source='int f(void) { return 9; }', best_verified=0 "
                   "WHERE id=1")
    stub_compile_eval(net, monkeypatch, cand=ForgedCand())
    out = net.revalidate_bests("fixture")
    assert "NOT reproduced" in out
    assert net.store.one("SELECT best_verified FROM functions WHERE id=1")["best_verified"] == 0
    assert "not reproduced" in net.store.one("SELECT text FROM notes WHERE function_id=1")["text"]
    stub_compile_eval(net, monkeypatch)  # reproduces: the trust flag is restored
    net.revalidate_bests("fixture")
    assert net.store.one("SELECT best_verified FROM functions WHERE id=1")["best_verified"] == 1


# ----------------------------------------------------------------------------- P3.4 agent summary

def scored_fixture(net, monkeypatch, code: bytes):
    """Give f1 real target bytes (the scored path) and make the compile yield a candidate with `code`."""
    from decompnet.elfobj import FunctionObj

    with net.store.tx() as db:
        db.execute("UPDATE functions SET target=?, fingerprint=NULL WHERE id=1", (b"\x03\xe0\x00\x08",))
    cand = FunctionObj("f1", "mips", False, code, [])
    stub_compile_eval(net, monkeypatch, cand=cand)
    return cand


def test_mismatch_text_lists_only_nonzero_counts_with_schema_and_coverage():
    from decompnet import scoring
    from decompnet.network import _mismatch_text

    summary = scoring.mismatch_summary({"register": 4, "stack_offset": 2}, bytes_match=False, score=10,
                                       hypotheses=["a spill differs"])
    assert _mismatch_text(summary) == (
        f"mismatch summary (schema v{scoring.SUMMARY_SCHEMA_VERSION}, coverage complete): "
        "register 4, stack_offset 2\nhypotheses: a spill differs")
    clean = _mismatch_text(scoring.mismatch_summary({}, bytes_match=False, score=1))
    assert "operand" not in clean and "no instruction-level differences" in clean and "hypotheses" not in clean


def test_a_scored_miss_carries_the_structured_mismatch_summary_and_keeps_the_score(net, alice, monkeypatch):
    import re

    from decompnet import scoring

    cand = scored_fixture(net, monkeypatch, b"\x00\x00\x00\x00\x03\xe0\x00\x08")  # an extra leading nop
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    out = net.attempt(alice, "f1", "int f(void) { return 1; }")
    assert out["matched"] is False
    assert out["score"] == scoring.diff(net._target_obj(fn, net.store.one("SELECT * FROM projects WHERE id=1")),
                                        cand).score  # the summary never changes the score
    assert re.search(rf"mismatch summary \(schema v{scoring.SUMMARY_SCHEMA_VERSION}, coverage complete\): "
                     r"[a-z_]+ \d+(, [a-z_]+ \d+)*", out["text"])
    assert "insertion 1" in out["text"]


def test_attempt_path_records_snapshot_validate_render_and_persist_spans_when_telemetry_is_on(net, alice, monkeypatch):
    from decompnet import telemetry

    telemetry.enable(True)
    try:
        telemetry.clear()
        scored_fixture(net, monkeypatch, b"\x00\x00\x00\x00\x03\xe0\x00\x08")  # a scored miss: render + persist
        assert net.attempt(alice, "f1", "int f(void) { return 1; }")["matched"] is False
        scored_fixture(net, monkeypatch, b"\x03\xe0\x00\x08")  # an exact match: the validity guard runs
        assert net.attempt(alice, "f1", "int f(void) { return 2; }")["matched"] is True
        names = set(telemetry.snapshot()["stage_ms"])
        assert {"attempt.snapshot", "attempt.validate", "attempt.render", "attempt.persist"} <= names
    finally:
        telemetry.enable(False)
        telemetry.clear()


def test_a_hash_only_miss_and_an_exact_match_carry_no_mismatch_summary(net, alice, monkeypatch):
    stub_compile_eval(net, monkeypatch, cand=ForgedCand())
    assert "mismatch summary" not in net.attempt(alice, "f1", "int f(void) { return 1; }")["text"]
    stub_compile_eval(net, monkeypatch)
    out = net.attempt(alice, "f1", "int f(void) { return 2; }")
    assert out["matched"] is True and "mismatch summary" not in out["text"]


# ----------------------------------------------------------------------------- 18.4 receipts

def test_hash_only_match_receipt_stores_the_object_digest_and_an_unverified_link(net, alice, monkeypatch):
    stub_compile_eval(net, monkeypatch)
    assert net.attempt(alice, "f1", "int f(void) { return 1; }")["matched"] is True
    r = net.store.one("SELECT * FROM verification_receipts WHERE function_id=1")
    assert r["candidate_object_digest"] == FP
    assert r["object_match"] == "fingerprint" and r["function_link_match"] == "unverified"


def test_scored_match_receipt_stores_the_object_digest_and_an_exact_link(net, alice, monkeypatch):
    cand = scored_fixture(net, monkeypatch, b"\x03\xe0\x00\x08")
    assert net.attempt(alice, "f1", "int f(void) { return 1; }")["matched"] is True
    r = net.store.one("SELECT * FROM verification_receipts WHERE function_id=1")
    assert r["candidate_object_digest"] == cand.fingerprint()
    assert r["object_match"] == "exact" and r["function_link_match"] == "exact"


def test_record_without_a_link_verdict_is_unverified_only_when_told_so(net, alice):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    net._record(fn, alice, 0, "int f(void) { return 1; }", 1, matched=True, compiled=True,
                snapshot=snapshot_of(net, fn), object_digest="abc", link_verdict=False)
    r = net.store.one("SELECT * FROM verification_receipts WHERE function_id=1")
    assert r["candidate_object_digest"] == "abc" and r["function_link_match"] == "unverified"


# ----------------------------------------------------------------------------- refused matches are reported

def _refuse_by_guard(net, monkeypatch):
    from decompnet import validity

    monkeypatch.setattr(net, "_validity", lambda fn, source: validity.Validity(
        ok=False, warnings=("'p' is used uninitialized in this function",), detail="'p' is used uninitialized"))


def _still_open_and_claimed(net, agent) -> bool:
    fn = net.store.one("SELECT status FROM functions WHERE id=1")
    claim = net.store.one("SELECT agent_id FROM claims WHERE function_id=1")
    return fn["status"] == "open" and claim is not None and claim["agent_id"] == agent.id


def test_a_byte_identical_match_the_validity_guard_refuses_is_not_reported_as_a_match(net, alice, monkeypatch):
    scored_fixture(net, monkeypatch, b"\x03\xe0\x00\x08")
    _refuse_by_guard(net, monkeypatch)
    out = net.attempt(alice, "f1", "int f(void) { int *p; return *p; }")
    assert out["matched"] is False and "MATCH!" not in out["text"]
    assert "byte-identical" in out["text"] and "refused" in out["text"] and "uninitialised use" in out["text"]
    assert "claim is kept" in out["text"] and _still_open_and_claimed(net, alice)


def test_a_hash_only_match_the_validity_guard_refuses_is_not_reported_as_a_match(net, alice, monkeypatch):
    stub_compile_eval(net, monkeypatch)
    _refuse_by_guard(net, monkeypatch)
    out = net.attempt(alice, "f1", "int f(void) { int *p; return *p; }")
    assert out["matched"] is False and "MATCH!" not in out["text"]
    assert "fingerprint" in out["text"] and "uninitialised use" in out["text"]
    assert _still_open_and_claimed(net, alice)


def test_a_match_that_goes_stale_inside_the_acceptance_transaction_is_not_reported_as_a_match(net, alice, monkeypatch):
    scored_fixture(net, monkeypatch, b"\x03\xe0\x00\x08")
    calls = {"n": 0}
    real = snapshots.check_current

    def check_current(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(*args, **kwargs)  # _judge's check passes...
        return False, "context changed"  # ...and the recheck inside _record's transaction fails

    monkeypatch.setattr(snapshots, "check_current", check_current)
    out = net.attempt(alice, "f1", "int f(void) { return 1; }")
    assert calls["n"] == 2
    assert out["matched"] is False and "MATCH!" not in out["text"] and "context changed" in out["text"]
    assert _still_open_and_claimed(net, alice)


def test_record_says_why_a_requested_match_was_not_accepted(net, alice):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    why = net._record(fn, alice, 0, "int f(void) { return 1; }", 1, matched=True, compiled=True)
    assert why and "no snapshot" in why  # a ROM-project match needs its snapshot
    assert net._record(fn, alice, 5, "int f(void) { return 2; }", 1, snapshot=snapshot_of(net, fn)) is None
    assert net._record(fn, alice, 0, "int f(void) { return 3; }", 1, matched=True, compiled=True,
                       snapshot=snapshot_of(net, fn)) is None  # accepted
    assert net.store.one("SELECT status FROM functions WHERE id=1")["status"] == "matched"


# ----------------------------------------------------------------------------- P4.1 calibration

def test_calibrate_profile_reports_and_never_changes_the_project_profile(net):
    before = net.store.one("SELECT config FROM projects WHERE id=1")["config"]
    out = net.calibrate_profile("fixture")  # the fixture has no open function with target bytes and a draft
    assert "nothing to calibrate" in out and "Nothing was changed" in out
    assert net.store.one("SELECT config FROM projects WHERE id=1")["config"] == before
