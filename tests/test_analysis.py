"""Analysis broker tests (plan A-01..A-07, P8.1-P8.7).

The provider is a fake that implements the exact Provider surface the PR-25 REA adapter must implement;
the direct-Ghidra provider's real admission logic is exercised against genuine Ghidra profile rules (no
Ghidra install needed: admission is pure Python). All concurrency tests are event-driven (no sleeps).
"""

import threading
import time
from pathlib import Path

import pytest

from decompnet import scoring
from decompnet.analysis import (Broker, Budget, CapabilityError, CandidateChange, DirectGhidraProvider,
                                EscalationInputs, EscalationPolicy, FunctionEvidence, ProviderError,
                                ProviderResult, StaleEvidenceError, TargetError, independent_sources)
from decompnet.analysis.broker import Provider
from decompnet.forks.ghidra import TargetProfile


def elf32le_object() -> bytes:
    h = bytearray(b"\x7fELF" + bytes([1, 1, 1]) + bytes(9))
    h += b"\x01\x00"  # e_type = ET_REL, little-endian
    h += bytes(2)
    return bytes(h) + bytes(64)


class FakeProvider(Provider):
    """Implements the Provider surface the broker drives; doubles as the reference implementation of the
    interface the PR-25 REA adapter must provide."""

    def __init__(self, name="fake", engine="fake-1.0"):
        self.name = name
        self.engine = engine
        self.capabilities_result = None
        self.capability_calls = []
        self.opened = []
        self.closed = []
        self.fetches = []
        self.refreshes = []
        self.flushes = []
        self.fail_open = False
        self.fail_fetch = False
        self.fetch_result = None
        self.fetch_handler = None
        self.handshake = None  # (started_event, release_event)
        self.honor_cancel = True

    def capabilities(self, target_profile, artifact=None):
        self.capability_calls.append((target_profile, artifact))
        if self.capabilities_result is not None:
            return self.capabilities_result
        from decompnet.analysis import CapabilityResult
        return CapabilityResult(True, self.name, (), profile={"auto": False, "kind": "object"})

    def open_session(self, session_key, profile):
        self.opened.append(session_key)
        if self.fail_open:
            self.fail_open = False  # fail exactly once: the retry must succeed
            raise ProviderError(f"{self.name}: cannot open session (simulated)")
        return {"session": session_key}

    def close_session(self, session_key, handle):
        self.closed.append(session_key)

    def fetch(self, session_key, handle, request, cancel):
        self.fetches.append({"session": session_key, "request": request, "cancel": cancel})
        if self.handshake is not None:
            self.handshake[0].set()  # the request reached the provider (event-driven synchronisation)
        if cancel is not None and self.honor_cancel:
            if not cancel.wait(timeout=5):  # waits for the owner's cancel event (event-driven)
                pass
            if cancel.is_set():
                return ProviderResult(status="cancelled", diagnostics=["cancelled by the caller"])
        elif self.handshake is not None:
            assert self.handshake[1].wait(timeout=5), "test failed to release the fetch handshake"
        if self.fail_fetch:
            self.fail_fetch = False
            raise ProviderError(f"{self.name}: fetch crashed (simulated)")
        if self.fetch_handler is not None:
            return self.fetch_handler(session_key, handle, request, cancel)
        if self.fetch_result is not None:
            return self.fetch_result
        functions = {}
        for name in request.functions:
            functions[f"{request.inputs.get('module', 'game')}::{name}"] = FunctionEvidence(
                function_key=f"{request.inputs.get('module', 'game')}::{name}", provider=self.name,
                engine=self.engine, correlation_group="", snapshot=request.snapshot, status="ok",
                coverage={"pseudocode": "ok"}, views={"pseudocode": {"status": "ok", "text": f"int {name}(void) {{}}"}},
                entry=0x1000)
        return ProviderResult(status="ok", functions=functions, wall_seconds=0.01)

    def refresh(self, session_key, scope, new_snapshot):
        self.refreshes.append((session_key, scope, new_snapshot))
        return 1

    def flush(self, session_key, *, view_invalidated, reason):
        self.flushes.append((session_key, view_invalidated, reason))


def make_broker(tmp_path, *, fake=None, max_sessions=2, idle_seconds=3600.0, max_pending=32):
    b = Broker(cache_dir=tmp_path / "cache", max_sessions=max_sessions, idle_seconds=idle_seconds,
               max_pending_per_session=max_pending)
    fake = fake or FakeProvider()
    b.register_provider(fake)
    return b, fake


def register(b, fake, tmp_path, module="game", profile=None, provider=None):
    obj = tmp_path / f"{module}.o"
    obj.write_bytes(bytes(64))
    profile = profile or {"kind": "object", "processor": "x86", "bits": 64, "endian": "little"}
    b.register_target(module, obj, profile, provider=provider)
    return obj


def wait_for_pending(b, count=1, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        states = b.session_states()
        if states and any(s["pending"] >= count for s in states.values()):
            return True
        time.sleep(0.005)
    return False


# ---------------------------------------------------------------------------- A-01

def test_identical_requests_share_one_import_and_are_attributed(tmp_path):
    b, fake = make_broker(tmp_path)
    register(b, fake, tmp_path)
    started, release = threading.Event(), threading.Event()
    fake.handshake = (started, release)
    results = {}

    def worker(tag):
        results[tag] = b.get_function_evidence("game::fn", ("calls",), "current", requester=tag)

    t1 = threading.Thread(target=worker, args=("agent-1",))
    t1.start()
    assert started.wait(5), "the first request never reached the provider"
    t2 = threading.Thread(target=worker, args=("agent-2",))
    t2.start()
    # event-driven: wait until the second caller is attached as a waiter of the shared request
    inflight = b.inflight_requests()
    assert len(inflight) == 1
    identity = next(iter(inflight))
    assert inflight[identity]["waiter_event"].wait(5), "the second caller never attached as a waiter"
    release.set()
    t1.join(5)
    t2.join(5)
    assert not t1.is_alive() and not t2.is_alive()
    one, two = results["agent-1"], results["agent-2"]
    assert one.status == "ok" and two.status == "ok"
    assert len(fake.fetches) == 1  # one import, shared
    assert not one.shared and two.shared
    assert one.requester == "agent-1" and two.requester == "agent-2"
    assert one.request_id != two.request_id
    assert one.correlation_group == two.correlation_group
    assert one.provider == two.provider == "fake"
    assert "game::fn" in one.functions and "game::fn" in two.functions


# ---------------------------------------------------------------------------- A-02

def test_easy_functions_never_start_a_provider(tmp_path):
    b, fake = make_broker(tmp_path)
    policy = b.policy
    twin = policy.decide(EscalationInputs(function_key="g::f", twin_available=True, m2c_tried=True))
    assert twin.kind == "twin_source" and not twin.needs_provider
    m2c = policy.decide(EscalationInputs(function_key="g::f", m2c_tried=False))
    assert m2c.kind == "m2c_draft" and not m2c.needs_provider
    assert fake.opened == [] and fake.fetches == []  # no session, no import, anywhere
    with pytest.raises(TargetError, match="never admitted"):
        b.get_function_evidence("nope::f", ("calls",))
    assert fake.opened == [] and fake.fetches == []  # unregistered targets fail without provider startup


# ---------------------------------------------------------------------------- A-03

def _mismatch(counts: dict, score: int = 10) -> dict:
    return scoring.mismatch_summary({k: 0 for k in scoring.COUNT_PENALTIES} | counts,
                                    bytes_match=False, score=score)


def test_structural_versus_register_only_routing():
    p = EscalationPolicy()
    reg = p.decide(EscalationInputs(function_key="g::f", m2c_tried=True, attempts=4,
                                    mismatch=_mismatch({"register": 2, "stack_offset": 1})))
    assert reg.kind == "permutation" and not reg.needs_provider
    struct = p.decide(EscalationInputs(function_key="g::f", m2c_tried=True, attempts=2,
                                       mismatch=_mismatch({"insertion": 1, "deletion": 2}, score=200)))
    assert struct.kind == "investigate_function" and struct.needs_provider
    early = p.decide(EscalationInputs(function_key="g::f", m2c_tried=True, attempts=1,
                                      mismatch=_mismatch({"insertion": 1}, score=100)))
    assert early.kind == "none" and not early.needs_provider  # not automatically after every attempt
    flow = p.decide(EscalationInputs(m2c_tried=True, control_flow_uncertain=True))
    assert flow.kind == "investigate_function"
    table = p.decide(EscalationInputs(m2c_tried=True, jump_table_uncertain=True))
    assert table.kind == "investigate_function"
    conflict = p.decide(EscalationInputs(m2c_tried=True, conflicting_cross_function_evidence=True))
    assert conflict.kind == "investigate_neighbourhood" and conflict.needs_provider
    decl = p.decide(EscalationInputs(m2c_tried=True, compile_errors=("missing declaration foo", "other")))
    assert decl.kind == "context_repair" and decl.details["declarations"] == ["missing declaration foo"]
    lit = p.decide(EscalationInputs(m2c_tried=True, data_literal_mismatch=True))
    assert lit.kind == "data_literal_check"
    calib = EscalationPolicy()
    for name in ("g::a", "g::b"):
        calib.decide(EscalationInputs(function_key=name, m2c_tried=True, compiler_profile_confidence=0.2))
    cal = calib.decide(EscalationInputs(function_key="g::c", m2c_tried=True, compiler_profile_confidence=0.2))
    assert cal.kind == "profile_calibration"
    rel = p.decide(EscalationInputs(m2c_tried=True, attempts=6))
    assert rel.kind == "release_cooldown"
    # configurable thresholds: analysis after one attempt when configured so
    eager = EscalationPolicy(attempts_before_analysis=1)
    assert eager.decide(EscalationInputs(m2c_tried=True, attempts=1,
                                         mismatch=_mismatch({"insertion": 1}, 100))).kind == "investigate_function"


def test_profile_calibration_needs_three_distinct_low_confidence_functions():
    p = EscalationPolicy()
    one = EscalationInputs(function_key="g::f", m2c_tried=True, compiler_profile_confidence=0.2)
    assert p.decide(one).kind == "none"  # a single function alone does not trigger calibration
    assert p.decide(one).kind == "none"  # the same function reporting again is still one function
    q = EscalationPolicy()
    for name in ("g::a", "g::b"):
        assert q.decide(EscalationInputs(function_key=name, m2c_tried=True,
                                         compiler_profile_confidence=0.2)).kind == "none"
    third = q.decide(EscalationInputs(function_key="g::c", m2c_tried=True, compiler_profile_confidence=0.2))
    assert third.kind == "profile_calibration"


# ---------------------------------------------------------------------------- A-04

def test_same_engine_routes_share_a_correlation_group_and_never_double_count(tmp_path):
    b = Broker()
    direct = FakeProvider(name="direct-ghidra", engine="ghidra-12.1.4")
    rea = FakeProvider(name="rea-ghidra", engine="ghidra-12.1.4")
    older = FakeProvider(name="older-ghidra", engine="ghidra-11.0")
    for p in (direct, rea, older):
        b.register_provider(p)
    register(b, direct, tmp_path, provider="direct-ghidra")
    b1 = b.get_function_evidence("game::fn", ("calls",), provider="direct-ghidra")
    b2 = b.get_function_evidence("game::fn", ("calls",), provider="rea-ghidra")
    b3 = b.get_function_evidence("game::fn", ("calls",), provider="older-ghidra")
    assert b1.correlation_group == b2.correlation_group
    assert independent_sources(b1.correlation_group, b2.correlation_group) is False
    assert b1.correlation_group != b3.correlation_group
    assert independent_sources(b1.correlation_group, b3.correlation_group) is True
    assert b1.provider == "direct-ghidra" and b2.provider == "rea-ghidra"  # wrappers differ, evidence correlates
    # new evidence is counted once per group: three requests, two underlying evidence identities
    assert b.metrics_summary()["new_evidence_groups"] == 2


def test_provider_failure_is_returned_and_never_silently_switched(tmp_path):
    b, fake = make_broker(tmp_path)
    second = FakeProvider(name="backup", engine="other-1.0")
    b.register_provider(second)
    register(b, fake, tmp_path, provider="fake")
    fake.fetch_result = ProviderResult(status="failed", diagnostics=["engine exploded"])
    bundle = b.get_function_evidence("game::fn", ("calls",))
    assert bundle.status == "failed" and bundle.provider == "fake"
    assert second.fetches == []  # no silent failover; an explicit retry decides


# ---------------------------------------------------------------------------- A-05

def test_budget_exceeded_preserves_the_ordinary_path_and_is_never_cached(tmp_path):
    b, fake = make_broker(tmp_path)
    register(b, fake, tmp_path)
    fake.fetch_result = ProviderResult(status="timeout", diagnostics=["wall budget spent"])
    bundle = b.get_function_evidence("game::fn", ("calls",), budget=Budget(wall_seconds=1.0))
    assert bundle.status == "budget_exceeded"
    fake.fetch_result = None  # default ok again
    again = b.get_function_evidence("game::fn", ("calls",), budget=Budget(wall_seconds=1.0))
    assert again.status == "ok" and len(fake.fetches) == 2  # nothing cached as a complete success
    assert b.policy.decide(EscalationInputs(twin_available=True)).kind == "twin_source"


def test_waiter_cancellation_never_strands_the_shared_work(tmp_path):
    b, fake = make_broker(tmp_path)
    register(b, fake, tmp_path)
    started, release = threading.Event(), threading.Event()
    fake.handshake = (started, release)
    results = {}

    def owner():
        results["owner"] = b.get_function_evidence("game::fn", ("calls",), requester="owner")

    t1 = threading.Thread(target=owner)
    t1.start()
    assert started.wait(5)
    waiter_cancel = threading.Event()

    def waiter():
        results["waiter"] = b.get_function_evidence("game::fn", ("calls",), requester="waiter",
                                                    cancel=waiter_cancel)

    t2 = threading.Thread(target=waiter)
    t2.start()
    inflight = b.inflight_requests()
    assert next(iter(inflight.values()))["waiter_event"].wait(5)
    waiter_cancel.set()
    t2.join(5)
    assert results["waiter"].status == "cancelled"  # the waiter leaves; the shared request continues
    release.set()
    t1.join(5)
    assert results["owner"].status == "ok"
    assert len(fake.fetches) == 1


def test_cancelling_the_active_shared_request_invalidates_the_session_and_recovers(tmp_path):
    b, fake = make_broker(tmp_path)
    register(b, fake, tmp_path)
    started, release = threading.Event(), threading.Event()
    fake.handshake = (started, release)
    results = {}
    owner_cancel = threading.Event()

    def owner():
        results["owner"] = b.get_function_evidence("game::fn", ("calls",), requester="owner",
                                                    cancel=owner_cancel)

    t1 = threading.Thread(target=owner)
    t1.start()
    assert started.wait(5), "the owner never reached the provider"
    inflight = b.inflight_requests()
    assert len(inflight) == 1

    def waiter():
        results["waiter"] = b.get_function_evidence("game::fn", ("calls",), requester="waiter")

    t2 = threading.Thread(target=waiter)
    t2.start()
    assert next(iter(b.inflight_requests().values()))["waiter_event"].wait(5)
    owner_cancel.set()  # the fake provider honours it and returns "cancelled"
    t1.join(5)
    t2.join(5)
    assert results["owner"].status == "cancelled"
    assert results["waiter"].status == "retryable"  # affected in-flight consumers marked retryable
    assert "waiter" in results["waiter"].diagnostics[0] or "cancelled" in results["waiter"].diagnostics[0]
    # the session was invalidated and is re-established explicitly on the next request
    assert any(s["state"] == "cancelled" for s in b.session_states().values())
    fake.handshake = None
    fake.fetch_result = None
    again = b.get_function_evidence("game::fn", ("calls",), requester="retry")
    assert again.status == "ok" and len(fake.opened) == 2


# ---------------------------------------------------------------------------- A-06

def _investigation_handler(calls=None):
    def handler(session_key, handle, request, cancel):
        functions = {}
        for name in request.functions:
            views = {
                "pseudocode": {"status": "ok", "text": f"int {name}(void) {{}}"},
                "instructions": {"items": [{"address": 0x1000, "size": 4, "asm": "mov"}]},
                "calls": {"items": list(calls or []) if name.endswith("fn") else []},
                "proposals": {"items": [{
                    "hypothesis": "the first parameter is signed, not unsigned",
                    "evidence_ids": ["game::fn:instructions:0"], "contradicting": [],
                    "requested_changes": [{"kind": "type", "target": "arg0", "before": "u32",
                                           "after": "s32", "origin": "decompiler", "complete": False}],
                    "confidence": 0.6}]},
            }
            functions[f"game::{name}"] = FunctionEvidence(
                function_key=f"game::{name}", provider="fake", engine="fake-1.0", correlation_group="",
                snapshot=request.snapshot, status="ok",
                coverage={"pseudocode": "ok", "instructions": "ok", "calls": "ok"},
                views=views, entry=0x1000)
        return ProviderResult(status="ok", functions=functions)
    return handler


def test_context_change_demotes_candidates_and_blocks_stale_application(tmp_path):
    b, fake = make_broker(tmp_path)
    register(b, fake, tmp_path)
    fake.fetch_handler = _investigation_handler()
    inv = b.propose_investigation("game::fn", "is the first parameter signed?", None, snapshot="s1")
    assert inv.status == "completed" and inv.snapshot == "s1"
    assert inv.facts and any(f.evidence_id.startswith("game::fn:") for f in inv.facts)
    assert inv.coverage["pseudocode"] == "ok"
    assert inv.ranked_hypotheses() and inv.ranked_hypotheses()[0].supporting
    assert inv.candidate_changes and inv.candidate_changes[0].expected_observation
    assert inv.budget_total.wall_seconds > 0 and inv.stopping_condition
    portfolio = b.portfolio("game::fn")
    record = portfolio.records[-1]
    assert record.snapshot == "s1" and record.change is not None
    portfolio.mark_current(record, "s1", scored=True)  # current best only after current-snapshot scoring
    assert portfolio.current() is record
    # the context changes mid-investigation
    assert b.refresh_evidence("game::fn", "s2") >= 1
    assert fake.refreshes and fake.refreshes[-1][2] == "s2"
    assert portfolio.current() is None  # old-snapshot current demoted...
    assert all(r.status == "historical" for r in portfolio.records)  # ...and retained as historical evidence
    with pytest.raises(StaleEvidenceError, match="s1"):
        portfolio.apply_change(record.change, "s2")  # stale facts cannot be applied as current
    fresh = CandidateChange(kind="type", target="arg0", before="u32", after="s32", snapshot="s2",
                            expected_observation="the diff changes", confidence=0.5)
    assert "protected edit path" in portfolio.apply_change(fresh, "s2")


def test_marking_current_without_scoring_is_refused(tmp_path):
    b, fake = make_broker(tmp_path)
    register(b, fake, tmp_path)
    fake.fetch_handler = _investigation_handler()
    b.propose_investigation("game::fn", "q?", None, snapshot="s1")
    record = b.portfolio("game::fn").records[-1]
    with pytest.raises(Exception, match="scored"):
        b.portfolio("game::fn").mark_current(record, "s1", scored=False)


# ---------------------------------------------------------------------------- A-07

def test_shared_session_failure_is_retryable_for_all_and_recovers(tmp_path):
    b, fake = make_broker(tmp_path)
    register(b, fake, tmp_path)
    started, release = threading.Event(), threading.Event()
    fake.handshake = (started, release)
    fake.fail_fetch = True  # the shared session crashes mid-fetch
    results = {}

    def owner():
        results["owner"] = b.get_function_evidence("game::fn", ("calls",), requester="owner")

    t1 = threading.Thread(target=owner)
    t1.start()
    assert started.wait(5)

    def waiter():
        results["waiter"] = b.get_function_evidence("game::fn", ("calls",), requester="waiter")

    t2 = threading.Thread(target=waiter)
    t2.start()
    assert next(iter(b.inflight_requests().values()))["waiter_event"].wait(5)

    def queued():  # a different request on the same session, queued behind the crashing one
        results["queued"] = b.get_function_evidence("game::other", ("calls",), requester="queued")

    t3 = threading.Thread(target=queued)
    t3.start()
    assert wait_for_pending(b), "the queued consumer never joined the session queue"
    release.set()
    for t in (t1, t2, t3):
        t.join(5)
    assert results["owner"].status == "retryable"
    assert results["waiter"].status == "retryable"
    assert results["queued"].status == "retryable"
    assert any("session failed" in d for d in results["owner"].diagnostics)
    # an explicit retry re-establishes the session and succeeds
    again = b.get_function_evidence("game::fn", ("calls",), requester="retry")
    assert again.status == "ok"
    assert len(fake.opened) == 2  # the failed session was replaced, not reused
    assert len(fake.closed) >= 1  # the old session's handle was disposed


def test_session_open_failure_is_retryable_and_recovers(tmp_path):
    b, fake = make_broker(tmp_path)
    register(b, fake, tmp_path)
    fake.fail_open = True
    first = b.get_function_evidence("game::fn", ("calls",))
    assert first.status == "retryable" and "failed to start" in first.diagnostics[0]
    second = b.get_function_evidence("game::fn", ("calls",))
    assert second.status == "ok" and len(fake.opened) == 2


# ---------------------------------------------------------------------------- neighbourhoods and queues

def test_neighbourhood_fetches_direct_callees_bounded(tmp_path):
    b, fake = make_broker(tmp_path)
    register(b, fake, tmp_path)
    calls = [{"address": 0x100, "target": 0x200, "name": "callee_a", "indirect": False},
             {"address": 0x104, "target": 0x300, "name": "callee_b", "indirect": False},
             {"address": 0x108, "target": None, "name": "", "indirect": True}]  # unresolved: never followed
    fake.fetch_handler = _investigation_handler(calls=calls)
    bundle = b.get_neighbourhood_evidence("game::fn", {"max_functions": 4}, "current")
    assert bundle.status == "ok"
    # the neighbourhood was one shared request including primary + resolved callees, indirect skipped
    assert fake.fetches[-1]["request"].functions == ("callee_a", "callee_b", "fn")
    assert set(bundle.functions) == {"game::fn", "game::callee_a", "game::callee_b"}


def test_session_queue_denial_is_recorded_with_a_reason(tmp_path):
    b, fake = make_broker(tmp_path, max_pending=1)
    register(b, fake, tmp_path)
    started, release = threading.Event(), threading.Event()
    fake.handshake = (started, release)
    results = {}

    def one():
        results["one"] = b.get_function_evidence("game::fn", ("calls",))

    t1 = threading.Thread(target=one)
    t1.start()
    assert started.wait(5)

    def two():
        results["two"] = b.get_function_evidence("game::other", ("calls",))

    t2 = threading.Thread(target=two)
    t2.start()
    assert wait_for_pending(b), "the second request never joined the queue"
    denied = b.get_function_evidence("game::third", ("calls",))
    assert denied.status == "denied" and any("queue full" in d["reason"] for d in b.denials())
    release.set()
    t1.join(5)
    t2.join(5)
    assert results["one"].status == "ok" and results["two"].status == "ok"
    assert fake.fetches[1]["request"].functions == ("other",)  # FIFO behind the first request


def test_idle_eviction_closes_sessions_with_recorded_reasons(tmp_path):
    b, fake = make_broker(tmp_path)
    register(b, fake, tmp_path, module="game")
    register(b, fake, tmp_path, module="other")
    b.get_function_evidence("game::fn", ("calls",))
    b.get_function_evidence("other::fn", ("calls",))
    assert len(b.session_states()) == 2
    for session in b._sessions.values():  # white-box: one session is old, one is fresh
        session.last_used -= 100000
    b._sessions[next(iter(b._sessions))].last_used += 100000  # keep the first one recent
    closed = b.close_idle_sessions()
    assert len(closed) == 1 and "idle" in closed[0]["reason"]
    assert len(b.session_states()) == 1
    assert any(closed[0]["session"] in key for key in fake.closed)  # the provider handle was disposed


def test_flush_metadata_maps_to_flush_and_reset(tmp_path):
    b, fake = make_broker(tmp_path)
    register(b, fake, tmp_path)
    b.get_function_evidence("game::fn", ("calls",))
    b.flush_metadata("game::fn", view_invalidated=False, reason="types changed")
    assert fake.flushes[-1] == (next(iter(b.session_states())), False, "types changed")
    b.flush_metadata("game::fn", view_invalidated=True, reason="overlay layout changed")
    assert fake.flushes[-1][1] is True and fake.flushes[-1][2] == "overlay layout changed"
    assert all(s["state"] == "cancelled" for s in b.session_states().values())


# ---------------------------------------------------------------------------- direct provider admission

def test_direct_ghidra_provider_admits_and_rejects_with_reasons(tmp_path):
    provider = DirectGhidraProvider(tmp_path / "cache")
    obj = tmp_path / "x.o"
    obj.write_bytes(elf32le_object())
    ok = provider.capabilities(TargetProfile(kind="object", processor="mips", bits=32, endian="little"), obj)
    assert ok.supported and ok.provider == "direct-ghidra"
    assert ok.profile["language_id"] == "MIPS:LE:32:default" and ok.profile["loader"] == "Elf"
    bad = provider.capabilities(TargetProfile(kind="object", processor="6502", bits=8, endian="little"), obj)
    assert not bad.supported and any("8-bit" in r for r in bad.reasons)  # class mismatch: explicit rejection
    unsupported = provider.capabilities(TargetProfile(kind="object", processor="6502", bits=8, endian="little"),
                                        tmp_path / "plain.bin")
    assert not unsupported.supported and any("unsupported" in r for r in unsupported.reasons)
    wrong_endian = provider.capabilities(TargetProfile(kind="object", processor="mips", bits=32, endian="big"), obj)
    assert not wrong_endian.supported and any("little-endian" in r for r in wrong_endian.reasons)


def test_broker_capabilities_and_target_admission(tmp_path):
    b = Broker()
    b.register_provider(DirectGhidraProvider(tmp_path / "cache"))
    obj = tmp_path / "x.o"
    obj.write_bytes(elf32le_object())
    r = b.capabilities(TargetProfile(kind="object", processor="mips", bits=32, endian="little"), obj)
    assert r.supported and r.provider == "direct-ghidra"
    with pytest.raises(CapabilityError, match="not admitted"):
        b.register_target("bad", obj, TargetProfile(kind="object", processor="6502", bits=8, endian="little"))
    with pytest.raises(CapabilityError, match="not registered"):
        b.capabilities(TargetProfile(kind="object", processor="mips", bits=32, endian="little"), obj,
                       provider="nope")


# ---------------------------------------------------------------------------- measurement (P8.7)

def test_rules_that_never_improve_are_auto_disabled():
    p = EscalationPolicy(disable_after_failures=3, min_cost_to_disable=1.0)
    for _ in range(3):
        p.observe("investigate_function", improved=False, cost=10.0)
    assert "investigate_function" in p.disabled
    assert "no verified improvement" in p.disabled["investigate_function"]
    stalled = p.decide(EscalationInputs(m2c_tried=True, control_flow_uncertain=True))
    assert stalled.kind == "none"  # the disabled rule falls through instead of adding cost
    p.enable("investigate_function")
    assert p.decide(EscalationInputs(m2c_tried=True, control_flow_uncertain=True)).kind == "investigate_function"
    healthy = EscalationPolicy(disable_after_failures=3, min_cost_to_disable=1.0)
    for _ in range(3):
        healthy.observe("investigate_function", improved=True, cost=10.0)
    assert "investigate_function" not in healthy.disabled


def test_metrics_record_cold_warm_reuse_and_outcomes(tmp_path):
    b, fake = make_broker(tmp_path)
    register(b, fake, tmp_path)
    fake.fetch_result = ProviderResult(status="ok", functions={
        "game::fn": FunctionEvidence(function_key="game::fn", provider="fake", engine="fake-1.0",
                                     correlation_group="", snapshot="current", status="ok",
                                     coverage={"pseudocode": "ok"},
                                     views={"pseudocode": {"status": "ok", "text": "int fn(void) {}"}})},
        reused=True, wall_seconds=1.5)
    bundle = b.get_function_evidence("game::fn", ("calls",))
    assert bundle.reused
    b.record_escalation(action="investigate_function", function_key="game::fn", cold_start=True,
                        wall_seconds=4.0, worker_seconds=3.0, evidence_reused=True, compiles_before=7,
                        compiles_after=8, verified_improvement=True, resolved_unknown=True, outcome="resolved")
    summary = b.metrics_summary()
    assert summary["records"] == 1 and summary["cold_starts"] == 1
    assert summary["evidence_reused"] == 1 and summary["compiles_before"] == 7
    assert summary["verified_improvements"] == 1 and summary["resolved_unknowns"] == 1


def test_propose_investigation_honours_evidence_refs_and_budget(tmp_path):
    b, fake = make_broker(tmp_path)
    register(b, fake, tmp_path)
    fake.fetch_result = ProviderResult(status="budget_exceeded", diagnostics=["wall budget spent"])
    inv = b.propose_investigation("game::fn", "which signedness?", ["ref:dossier:1"], budget=Budget(wall_seconds=1.0),
                                  snapshot="s9")
    assert inv.status == "budget_exceeded"
    assert any("ref:dossier:1" in f.evidence_id for f in inv.facts)
    assert inv.budget_total.wall_seconds == 1.0
