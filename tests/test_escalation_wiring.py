"""A.3 escalation wiring: the analysis broker's policy consulted from the claim/stall path (P8.4).

- stall_note: once an agent is actually stalled (same threshold logic as before), the last attempt's
  structured mismatch evidence is derived (real recompile+diff when the attempt's snapshot is still
  current) and EscalationPolicy picks the next action (permutation vs investigation vs release),
  appended to the existing nudge.
- claim_row: ordering is untouched and the claim/brief hot path NEVER touches the analysis broker
  (A-02: easy functions must not start providers).
- Network.escalate(function): a bounded, provider-optional broker investigation (budget-capped,
  snapshot-bound to the function revision).

macOS-runnable: the end-to-end test compiles with clang (config-supplied command), no host gcc.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from decompnet import scoring, snapshots
from decompnet.analysis import (Broker, CapabilityResult, EscalationInputs, EscalationPolicy, FunctionEvidence,
                                ProviderResult, rea)
from decompnet.analysis.broker import DirectGhidraProvider, Provider
from decompnet.analysis.contracts import ProviderRequest
from decompnet.elfobj import FunctionObj, extract_function
from decompnet.forks import ghidra
from decompnet.network import Network

FIXTURE_C = "int add3(int a, int b, int c) { return a + b + c; }\n"
STRUCTURAL_MISS = "int add3(int a, int b, int c) { a += 1; return a + b + c; }\n"
NEAR_REGISTER = "int add3(int a, int b, int c) { int t = a + b; return t + c; }\n"
CC = ["clang", "-target", "x86_64-unknown-linux-gnu", "-c", "-I{include}", "{src}", "-o", "{obj}"]


@pytest.fixture(autouse=True)
def no_default_providers(monkeypatch):
    """Escalation tests register their own providers: neither a local Ghidra install nor the REA flag
    may add providers behind their back (tests that cover the default wiring switch them on)."""
    monkeypatch.setattr(ghidra, "available", lambda: False)
    monkeypatch.delenv("DECOMPNET_REA_PROVIDER", raising=False)


def make_network(tmp_path: Path, *, arch: str = "mips", config: str = "{}") -> Network:
    net = Network(tmp_path / "net.db")
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'fixture', 'fixture', 'escalation fixture', ?, 0, ?, ?)",
                   (arch, str(tmp_path), config))
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, 'int add3(int, int, int);', NULL, 'fixture', 0, '')")
    return net


def add_function(net: Network, *, name="add3", target: bytes | None = None, relocs="[]",
                 n_insns: int = 4, seed=None, best_score=None):
    with net.store.tx() as db:
        db.execute(
            "INSERT INTO functions(project_id, name, size, n_insns, target, relocs, asm, seed, status, "
            "best_score, best_source, best_agent_id, attempts, abandons) VALUES(1,?,?,?,?,?,?,?, 'open', ?, ?, NULL, 0, 0)",
            (name, 64, n_insns, target, relocs, "add3:", seed, best_score, best_score and f"best {best_score}"),
        )
    return net.store.one("SELECT * FROM functions WHERE name=?", name)


def add_agent(net: Network, name="alice") :
    return net.authenticate(net.register_agent(name))


def record_attempt(net: Network, fn_id: int, agent_id: int, source: str, score: int,
                   *, compiled: int = 1, snapshot: dict | None = None):
    """One attempt row (plus its snapshot row, capture-shaped when snapshot is given)."""
    with net.store.tx() as db:
        cur = db.execute("INSERT INTO attempts(function_id,agent_id,score,compiled,context_version,source,"
                         "created_at,via) VALUES(?,?,?,?,1,?,?,?)",
                         (fn_id, agent_id, score, compiled, source, 0, "agent"))
        attempt_id = cur.lastrowid
        if snapshot is not None:
            db.execute(
                "INSERT INTO attempt_snapshots(attempt_id, project_id, function_id, function_revision, "
                "context_version, context_digest, target_digest, compiler_digest, scorer_version, "
                "policy_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, snapshot["project_id"], snapshot["function_id"], snapshot["function_revision"],
                 snapshot["context_version"], snapshot["context_digest"], snapshot["target_digest"],
                 snapshot["compiler_digest"], snapshot["scorer_version"], snapshot["policy_version"]))
    return attempt_id


def stall_rows(net: Network, fn_id: int, agent_id: int, *, scores=(50, 50, 50, 50, 50),
               source: str = "int add3(int a,int b,int c){return a+b+c;}"):
    for score in scores:
        record_attempt(net, fn_id, agent_id, source, score)


class FakeProvider(Provider):
    """Counting provider for hot-path and escalate tests: records every broker touch."""

    name = "fake"
    engine = "fake-1.0"

    def __init__(self, *, capability=None, evidence=None):
        self.capability = capability if capability is not None else CapabilityResult(True, "fake")
        self.evidence = evidence  # callable(request) -> ProviderResult, default below
        self.opens = 0
        self.fetches = 0
        self.closes = 0
        self.capability_calls = 0

    def capabilities(self, target_profile, artifact=None):
        self.capability_calls += 1
        return self.capability

    def open_session(self, session_key, profile):
        self.opens += 1
        return {"session": session_key}

    def close_session(self, session_key, handle):
        self.closes += 1

    def fetch(self, session_key, handle, request: ProviderRequest, cancel=None):
        self.fetches += 1
        if self.evidence is not None:
            return self.evidence(request)
        functions = {}
        for name in request.functions:
            functions[f"fixture::{name}"] = FunctionEvidence(
                function_key=f"fixture::{name}", provider=self.name, engine=self.engine,
                correlation_group="", snapshot=request.snapshot, status="ok",
                coverage={"pseudocode": "ok", "instructions": "ok", "calls": "ok"},
                views={"pseudocode": {"status": "ok", "text": f"int {name}(void) {{}}"},
                       "instructions": {"items": [{"address": 0x1000, "size": 4, "asm": "nop"}]},
                       "proposals": {"items": [{
                           "hypothesis": f"{name} is signed, not unsigned",
                           "evidence_ids": [f"fixture::{name}:instructions:0"], "contradicting": [],
                           "requested_changes": [{"kind": "type", "target": "arg0", "before": "u32",
                                                  "after": "s32", "origin": "decompiler",
                                                  "complete": False}],
                           "confidence": 0.6}]}})
        return ProviderResult(status="ok", functions=functions)

    def refresh(self, session_key, scope, new_snapshot):
        return 1

    def flush(self, session_key, *, view_invalidated, reason):
        pass


# ---------------------------------------------------------------------------- stall_note (A.3.1)

def test_stall_note_keeps_the_existing_nudge_and_early_return(tmp_path, monkeypatch):
    net = make_network(tmp_path)
    fn = add_function(net)
    alice = add_agent(net)
    stall_rows(net, fn["id"], alice.id)
    net._escalation_policy = EscalationPolicy()
    out = net.stall_note(fn["id"], alice.id)
    assert "No better in your last" in out  # the reviewed nudge survives
    with net.store.tx() as db:
        db.execute("UPDATE functions SET n_insns=300 WHERE id=?", (fn["id"],))
    assert net.stall_note(fn["id"], alice.id) == ""  # a big one: not before 10


def test_stall_note_recommends_permutation_for_register_only_mismatch(tmp_path, monkeypatch):
    net = make_network(tmp_path)
    fn = add_function(net, seed="/* m2c */ int add3(int a, int b, int c);")  # m2c already tried
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    alice = add_agent(net)
    ctx = net._context(1, fn["unit"])
    snap = snapshots.capture(fn, proj, ctx)
    stall_rows(net, fn["id"], alice.id, scores=(50, 50, 50, 50, 50))
    record_attempt(net, fn["id"], alice.id, NEAR_REGISTER, 60, snapshot=snap)  # non-improving last attempt

    def fake_compile_and_evaluate(fn_row, proj_row, source, ctx_row):
        rows = [(" ", "mov eax, edi", "mov eax, edi"), ("r", "add eax, esi", "add eax, ebx"),
                ("s", "sub rsp, 8", "sub rsp, 16"), (" ", "ret", "ret")]
        return SimpleNamespace(ok=True), None, None, [], scoring.DiffResult(7, 400, rows, False)

    monkeypatch.setattr(net, "_compile_and_evaluate", fake_compile_and_evaluate)
    net._escalation_policy = EscalationPolicy()
    out = net.stall_note(fn["id"], alice.id)
    assert "PERMUTATION" in out and "register/stack" in out
    assert "escalate_function" not in out


def test_stall_note_recommends_investigation_for_structural_mismatch(tmp_path, monkeypatch):
    net = make_network(tmp_path)
    fn = add_function(net, seed="/* m2c */")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    alice = add_agent(net)
    snap = snapshots.capture(fn, proj, net._context(1, fn["unit"]))
    stall_rows(net, fn["id"], alice.id, scores=(50, 50, 50, 50))
    record_attempt(net, fn["id"], alice.id, STRUCTURAL_MISS, 105, snapshot=snap)  # non-improving last attempt

    def fake_compile_and_evaluate(fn_row, proj_row, source, ctx_row):
        rows = [(" ", "mov eax, edi", "mov eax, edi"), (">", "", "add edi, 1"),
                (" ", "ret", "ret")]
        return SimpleNamespace(ok=True), None, None, [], scoring.DiffResult(100, 300, rows, False)

    monkeypatch.setattr(net, "_compile_and_evaluate", fake_compile_and_evaluate)
    policy = EscalationPolicy()
    net._escalation_policy = policy
    out = net.stall_note(fn["id"], alice.id)
    assert "TARGETED INVESTIGATION" in out and 'escalate_function("add3")' in out
    # the policy really saw the structured mismatch
    assert policy.structural(EscalationInputs(mismatch=scoring.mismatch_summary(
        {"insertion": 1, "register": 0, "stack_offset": 0, "operand": 0, "deletion": 0, "reorder": 0},
        bytes_match=False, score=100)))


def test_stall_note_recommends_release_when_no_progress_and_no_evidence(tmp_path, monkeypatch):
    net = make_network(tmp_path)
    fn = add_function(net, seed="/* m2c */")
    alice = add_agent(net)
    stall_rows(net, fn["id"], alice.id, scores=(60, 60, 60, 60, 60, 60, 60))  # 7 non-improving
    # no snapshot row: the last attempt has no current structured evidence -> policy sees mismatch None
    net._escalation_policy = EscalationPolicy()
    out = net.stall_note(fn["id"], alice.id)
    assert "RELEASE" in out and "notes" in out


def test_stall_note_uses_the_real_clang_compile_and_diff_end_to_end(tmp_path, monkeypatch):
    """A real end-to-end stall: the last attempt's source is recompiled with the project's own
    clang command and re-diffed; the derived summary drives the policy. macOS-runnable (clang)."""
    if subprocess.run(["clang", "--version"], capture_output=True).returncode != 0:
        pytest.skip("clang is not available")
    config = json.dumps({"compiler": {"cmd": CC, "cache": False}})
    net = make_network(tmp_path, arch="x86_64", config=config)
    # the fixture's target bytes: the real compiled reference function
    ref_obj = subprocess.run(["clang", "-target", "x86_64-unknown-linux-gnu", "-c", "-x", "c", "-",
                              "-o", str(tmp_path / "ref.o")], input=FIXTURE_C.encode(), capture_output=True)
    assert ref_obj.returncode == 0, ref_obj.stderr.decode()
    reference = extract_function((tmp_path / "ref.o").read_bytes(), "add3")
    assert reference is not None
    n_insns = len(scoring.disassemble(FunctionObj(name="add3", arch="x86_64", big_endian=False,
                                                  code=reference.code, relocs=[])))
    fn = add_function(net, target=reference.code, relocs=json.dumps([r.to_json() for r in reference.relocs]),
                      n_insns=n_insns, seed="/* m2c */")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    alice = add_agent(net)
    snap = snapshots.capture(fn, proj, net._context(1, fn["unit"]))
    stall_rows(net, fn["id"], alice.id, scores=(50, 50, 50, 50))
    record_attempt(net, fn["id"], alice.id, STRUCTURAL_MISS, 110, snapshot=snap)  # non-improving last attempt
    net._escalation_policy = EscalationPolicy()
    out = net.stall_note(fn["id"], alice.id)
    assert "TARGETED INVESTIGATION" in out  # the real diff saw the inserted instruction


# ---------------------------------------------------------------------------- claim_row (A.3.2)

def test_claim_row_never_touches_the_analysis_broker(tmp_path):
    """A-02: the claim/brief hot path performs zero broker activity, even with a provider attached."""
    net = make_network(tmp_path)
    for i in range(1, 4):
        with net.store.tx() as db:
            db.execute("INSERT INTO functions(project_id, name, size, n_insns, target, relocs, asm, seed, "
                       "status, best_score, attempts, abandons) VALUES(1,?,64,100,NULL,'[]','',NULL,'open',?,0,0)",
                       (f"fn_{i:02d}", i * 10))
    fake = FakeProvider()
    broker = net.analysis_broker()
    broker.register_provider(fake)
    alice = add_agent(net)
    row = net.claim_row(alice, strategy="closest")
    assert row is not None
    assert fake.opens == 0 and fake.fetches == 0 and fake.capability_calls == 0
    brief = net.claim(alice)  # renewed claim + brief
    assert fake.opens == 0 and fake.fetches == 0


def test_brief_shows_wall_registry_rows_when_a_walled_function_is_claimed(tmp_path):
    net = make_network(tmp_path)
    fn = add_function(net)
    with net.store.tx() as db:
        db.execute("INSERT INTO function_walls(function_id, signature, reason, at) VALUES(?,?,?,?)",
                   (fn["id"], "wall-1", "blocked by a jump table I couldn't reproduce", time.time()))
    alice = add_agent(net)
    brief = net.claim(alice, function="add3")
    assert "Known walls" in brief and "jump table" in brief


def test_claim_ordering_is_unchanged_with_an_attached_broker(tmp_path):
    net = make_network(tmp_path)
    with net.store.tx() as db:
        for name, score, insns in (("a", None, 4), ("b", 40, 4), ("c", 20, 4)):
            db.execute("INSERT INTO functions(project_id, name, size, n_insns, target, relocs, asm, seed, "
                       "status, best_score, attempts, abandons) VALUES(1,?,64,?,NULL,'[]','',NULL,'open',?,0,0)",
                       (name, insns, score))
    fake = FakeProvider()
    net.analysis_broker().register_provider(fake)
    alice = add_agent(net)
    # 'easiest': smallest first (unchanged SQL) — a has best_score NULL -> first
    assert net.claim_row(alice, strategy="easiest")["name"] == "a"
    assert fake.fetches == 0


# ---------------------------------------------------------------------------- escalate (A.3.3)

def test_escalate_returns_a_budget_capped_investigation(tmp_path):
    net = make_network(tmp_path, arch="mips")
    fn = add_function(net, target=bytes([0x27, 0xbd, 0xff, 0xe0] * 4))
    fake = FakeProvider()
    net.analysis_broker().register_provider(fake)
    out = net.escalate("add3", budget={"wall_seconds": 5.0, "token_limit": 1000})
    assert "How should add3 be changed" in out
    assert "rev-1" in out  # snapshot-bound to the function revision (A-06)
    assert "budget 5s wall" in out
    assert fake.fetches == 1
    assert fake.opens == 1
    request = fake  # the request budget was capped
    assert net.analysis_broker().metrics_summary()["records"] >= 1
    portfolio = net.analysis_broker().portfolio("fixture::add3")
    assert portfolio.records and portfolio.records[-1].snapshot == "rev-1"


def test_escalate_without_providers_returns_explicit_guidance(tmp_path):
    net = make_network(tmp_path, arch="mips")
    add_function(net, target=bytes([0x27, 0xbd, 0xff, 0xe0] * 4))
    out = net.escalate("add3")
    assert "no provider registered" in out.lower()
    assert "register" in out or "provider" in out


def test_escalate_unsupported_arch_is_typed_and_never_guessed(tmp_path):
    net = make_network(tmp_path, arch="6502")
    add_function(net, target=bytes(16))
    out = net.escalate("add3")
    assert "no admitted Ghidra profile" in out and "6502" in out


def test_escalate_admission_failure_is_reported_not_swallowed(tmp_path):
    net = make_network(tmp_path, arch="mips")
    add_function(net, target=bytes(16))
    fake = FakeProvider(capability=CapabilityResult(False, "fake", ("unsupported language combo",)))
    net.analysis_broker().register_provider(fake)
    out = net.escalate("add3")
    assert "unsupported language combo" in out


def test_app_tools_reach_network_escalate_and_calibrate_profile(tmp_path, monkeypatch):
    from decompnet.app import tools

    net = make_network(tmp_path)
    calls = []
    monkeypatch.setattr(Network, "escalate", lambda self, function, **kw: calls.append(("escalate", function, kw)) or "esc")
    monkeypatch.setattr(Network, "calibrate_profile",
                        lambda self, project=None, **kw: calls.append(("calibrate", project, kw)) or "cal")
    assert {"escalate_function", "calibrate_profile"} <= set(tools.tool_names(False))
    assert tools.HANDLERS["escalate_function"](net, None, "fixture", function="add3", question="why?") == "esc"
    assert tools.HANDLERS["escalate_function"](net, None, "fixture", function="add3") == "esc"
    assert tools.HANDLERS["calibrate_profile"](net, None, "fixture") == "cal"
    assert tools.HANDLERS["calibrate_profile"](net, None, "fixture", function="add3") == "cal"
    assert calls == [
        ("escalate", "add3", {"question": "why?", "project": "fixture"}),
        ("escalate", "add3", {"question": None, "project": "fixture"}),
        ("calibrate", "fixture", {"functions": None}),
        ("calibrate", "fixture", {"functions": ["add3"]}),
    ]


# ---------------------------------------------------------------------------- default providers (P8/P9)

def test_direct_ghidra_provider_is_registered_only_when_ghidra_is_available(tmp_path, monkeypatch):
    monkeypatch.setenv("DECOMPNET_CACHE_DIR", str(tmp_path / "cache"))
    assert make_network(tmp_path).analysis_broker()._providers == {}  # fixture: Ghidra unavailable
    (tmp_path / "b").mkdir()
    monkeypatch.setattr(ghidra, "available", lambda: True)
    providers = make_network(tmp_path / "b").analysis_broker()._providers
    assert list(providers) == ["direct-ghidra"] and isinstance(providers["direct-ghidra"], DirectGhidraProvider)


def test_a_failing_provider_setup_never_breaks_the_broker(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("probe failed")

    monkeypatch.setattr(ghidra, "available", broken)
    monkeypatch.setenv("DECOMPNET_REA_PROVIDER", "1")
    monkeypatch.setattr(rea, "available", broken)
    broker = make_network(tmp_path).analysis_broker()
    assert broker._providers == {}


def test_rea_provider_is_off_by_default_and_needs_the_flag_and_rea(tmp_path, monkeypatch):
    monkeypatch.setattr(rea, "available", lambda: True)
    monkeypatch.setattr(rea, "ReaProvider", lambda: FakeProvider())
    assert make_network(tmp_path).analysis_broker()._providers == {}  # flag unset
    monkeypatch.setenv("DECOMPNET_REA_PROVIDER", "1")
    (tmp_path / "b").mkdir()
    assert list(make_network(tmp_path / "b").analysis_broker()._providers) == ["fake"]
    monkeypatch.setattr(rea, "available", lambda: False)
    (tmp_path / "c").mkdir()
    assert make_network(tmp_path / "c").analysis_broker()._providers == {}  # flag on, REA missing


# ---------------------------------------------------------------------------- outcome feedback and sessions

def escalated(tmp_path: Path, **function_kwargs):
    net = make_network(tmp_path, arch="mips")
    fn = add_function(net, target=bytes([0x27, 0xbd, 0xff, 0xe0] * 4), **function_kwargs)
    net.analysis_broker().register_provider(FakeProvider())
    net.escalate("add3", budget={"wall_seconds": 5.0, "token_limit": 1000})
    return net, fn


def investigations_seen(broker) -> dict:
    return broker.policy.rule_stats.get("investigate_function", {"investigations": 0, "improvements": 0})


def bare_broker(tmp_path: Path, fake: FakeProvider, *, modules=("fixture",), **broker_kwargs):
    broker = Broker(**broker_kwargs)
    broker.register_provider(fake)
    artifact = tmp_path / "art.bin"
    artifact.write_bytes(bytes(16))
    for module in modules:
        broker.register_target(module, artifact, {}, mapping={"n": 1})
    return broker, artifact


def test_escalation_reports_improved_when_the_function_gets_a_better_score(tmp_path):
    net, fn = escalated(tmp_path)
    broker = net.analysis_broker()
    assert investigations_seen(broker)["investigations"] == 0  # nothing is observed at proposal time
    assert len(net._escalation_ids) == 1
    net._record(fn, add_agent(net), 900, "int add3(void) { return 0; }", 1)
    stats = investigations_seen(broker)
    assert stats["investigations"] == 1 and stats["improvements"] == 1
    assert broker.metrics_summary()["verified_improvements"] == 1
    assert broker._metrics.records[-1].outcome.endswith(":improved")
    assert net._escalation_ids == {}  # the entry is dropped once reported


def test_escalation_without_a_better_score_keeps_waiting_then_reports_unimproved_on_release(tmp_path):
    net, fn = escalated(tmp_path, best_score=500)
    broker = net.analysis_broker()
    alice = add_agent(net)
    net._record(fn, alice, 900, "int add3(void) { return 1; }", 1)  # worse than the best: no improvement
    assert investigations_seen(broker)["investigations"] == 0 and len(net._escalation_ids) == 1
    with net.store.tx() as db:
        db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], alice.id, time.time(), time.time() + 3600))
    net.release(alice, "add3", "")
    stats = investigations_seen(broker)
    assert stats["investigations"] == 1 and stats["improvements"] == 0
    assert broker.metrics_summary()["verified_improvements"] == 0
    assert broker._metrics.records[-1].outcome.endswith(":no_improvement")
    assert net._escalation_ids == {}


def test_report_outcome_is_once_per_investigation_and_unknown_ids_are_ignored(tmp_path):
    broker, _ = bare_broker(tmp_path, FakeProvider())
    inv = broker.propose_investigation("fixture::add3", "q?", None, snapshot="s1")
    assert broker.report_outcome("no-such-investigation", True) is False
    assert broker.report_outcome(inv.investigation_id, True) is True
    assert broker.report_outcome(inv.investigation_id, False) is False
    assert investigations_seen(broker)["investigations"] == 1


def test_cold_start_is_true_only_for_the_call_that_started_the_session(tmp_path):
    broker, _ = bare_broker(tmp_path, FakeProvider())
    broker.propose_investigation("fixture::add3", "q?", None, snapshot="s1")
    broker.propose_investigation("fixture::add3", "q?", None, snapshot="s1")  # session reused
    broker.close_session(next(iter(broker.session_states())))
    broker.propose_investigation("fixture::add3", "q?", None, snapshot="s1")  # a fresh session again
    assert [r.cold_start for r in broker._metrics.records] == [True, False, True]


def test_closing_a_busy_session_drains_it_then_closes(tmp_path):
    started, release = threading.Event(), threading.Event()

    def slow(request):
        started.set()
        release.wait(10)
        return ProviderResult(status="ok", functions={})

    fake = FakeProvider(evidence=slow)
    broker, _ = bare_broker(tmp_path, fake)
    results = {}
    worker = threading.Thread(target=lambda: results.update(
        first=broker.get_function_evidence("fixture::add3", ("calls",))))
    worker.start()
    try:
        assert started.wait(10)
        key, session = next(iter(broker._sessions.items()))
        assert session.state == "ready"
        assert broker.close_session(key) is True
        assert session.state == "draining" and broker.session_states()[key]["state"] == "draining"
        denied = broker.get_function_evidence("fixture::other", ("calls",))  # new requests are denied
        assert denied.status == "denied"
        assert fake.fetches == 1 and fake.closes == 0  # the in-flight request finishes before closing
    finally:
        release.set()
        worker.join(10)
    assert results["first"].status == "ok"
    assert session.state == "closed" and fake.closes == 1
    assert key not in broker.session_states()


def test_per_project_session_quota_evicts_inside_the_project_only(tmp_path):
    assert Broker(max_sessions=3).max_sessions_per_project == 3  # default: the global cap
    fake = FakeProvider()
    broker, artifact = bare_broker(tmp_path, fake, modules=("fixture", "other"), max_sessions=4,
                                   max_sessions_per_project=1)
    broker.get_function_evidence("fixture::add3", ("calls",))
    broker.get_function_evidence("other::add3", ("calls",))
    assert len(broker.session_states()) == 2
    broker.register_target("fixture", artifact, {}, mapping={"n": 2})  # a second session for one project
    broker.get_function_evidence("fixture::add3", ("calls",))
    states = broker.session_states()
    assert sorted(key.split(":")[0] for key in states) == ["fixture", "other"]
    assert fake.opens == 3 and fake.closes == 1
    assert any("project session quota" in d["reason"] for d in broker.denials())
