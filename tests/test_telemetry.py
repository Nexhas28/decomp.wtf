"""Stage telemetry (plan P0.3 / PR-02): disabled-by-default no-op instrumentation, thread-local
stages, counters and aggregate worker/wall time. Stub-based."""

from __future__ import annotations

import threading

import pytest

from decompnet import telemetry
from decompnet.network import Network

from .test_permute import make_project


def test_disabled_by_default_and_noop():
    assert not telemetry.enabled()
    assert telemetry.snapshot() == {"enabled": False}
    with telemetry.span("x"):
        pass
    telemetry.count("y")
    assert telemetry.snapshot() == {"enabled": False}


def test_spans_counters_and_aggregates():
    telemetry.enable(True)
    try:
        telemetry.clear()
        with telemetry.span("compile", hit=False):
            pass
        with telemetry.span("judge"):
            pass
        telemetry.count("attempt.record", 2)
        s = telemetry.snapshot()
        assert s["enabled"] and s["stage_count"] == 2
        assert set(s["stage_ms"]) == {"compile", "judge"}
        assert s["stage_ms"]["compile"]["n"] == 1
        assert s["counters"]["attempt.record"] == 2
        assert s["wall_ms"] >= s["worker_ms"] >= 0
    finally:
        telemetry.enable(False)
        telemetry.clear()


def test_stages_are_thread_local():
    telemetry.enable(True)
    try:
        telemetry.clear()
        seen = {}

        def worker(tag):
            with telemetry.span(f"stage-{tag}"):
                pass
            seen[tag] = telemetry.snapshot()["stage_count"]

        t = threading.Thread(target=worker, args=("a",))
        t.start()
        t.join()
        worker("b")
        assert seen["a"] == 1 and seen["b"] == 1
    finally:
        telemetry.enable(False)
        telemetry.clear()


def test_attempt_path_records_stages_when_enabled(tmp_path, monkeypatch):
    net = Network(tmp_path / "tel.db")
    make_project(net, 2)
    alice = net.authenticate(net.register_agent("alice"))
    fn = net.store.one("SELECT * FROM functions WHERE id=1")

    class FakeResult:
        ok = True
        log = ""
        obj = b"x"

    net._compile = lambda proj, header, source, fn=None: FakeResult()
    net._evaluate = lambda fn, proj, obj: (None, None, [])

    telemetry.enable(True)
    try:
        telemetry.clear()
        net.attempt(alice, fn["name"], "int func_001(void) { return 1; }")
        s = telemetry.snapshot()
        names = {st["name"] for st in _stages()}
        assert {"attempt.claim", "attempt.compile", "attempt.evaluate"} <= names
        assert s["counters"].get("attempt.record", 0) >= 1
    finally:
        telemetry.enable(False)
        telemetry.clear()


def _stages():
    import collections

    return list(getattr(telemetry._local, "recorder", None).stages if getattr(telemetry._local, "recorder", None)
                else [])
