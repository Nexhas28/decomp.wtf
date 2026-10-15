"""Upstream decomp-permuter adapter (plan P5.7 / tests P-07, P-08): advisory external scores,
coordinator re-verification, bounded single-flight runs, versioned job specs. Stub-based: a fake
external tool script stands in for upstream; no decomp-permuter needed."""

from __future__ import annotations

import json
import sys
import time

import pytest

from decompnet import search
from decompnet.network import Network
from decompnet.search import external
from decompnet.search.policy import SearchPolicyError

from .test_permute import make_project


def fake_tool(tmp_path, payload: dict, delay: float = 0.0):
    """A stand-in upstream tool: writes scores.json (and optionally sleeps)."""
    script = tmp_path / "fake-permuter.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys, time\n"
        f"time.sleep({delay})\n"
        f"pathlib.Path('scores.json').write_text(json.dumps({json.dumps(payload)}))\n")
    script.chmod(0o755)
    return str(script)


def test_availability_is_an_admission_gate(tmp_path):
    assert external.discover(str(tmp_path / "absent")) is None
    assert not external.available(str(tmp_path / "absent"))
    tool = fake_tool(tmp_path, {"scores": {}})
    assert external.discover(tool) == tool
    assert external.available(tool)


def test_run_parses_the_tool_contract(tmp_path):
    tool = fake_tool(tmp_path, {"scores": {"f": 3}, "candidates": {"f": "int f(void){return 0;}"}})
    r = external.run(tool, tmp_path, timeout=30)
    assert r.status == "ok" and r.scores == {"f": 3}
    assert r.candidates == {"f": "int f(void){return 0;}"}


def test_run_times_out_and_never_publishes(tmp_path):
    tool = fake_tool(tmp_path, {"scores": {"f": 0}}, delay=30)
    r = external.run(tool, tmp_path, timeout=1)
    assert r.status == "timeout" and "1s" in r.log
    assert not (tmp_path / "scores.json").exists()


def test_bad_output_is_an_error_not_a_success(tmp_path):
    tool = fake_tool(tmp_path, {"not": "the contract"})
    assert external.run(tool, tmp_path, timeout=30).status == "error"


def test_one_external_run_at_a_time(tmp_path):
    """P5.7 step 6: no nested oversubscription — a second run while one is active is refused."""
    import threading

    release = threading.Event()
    entered = threading.Event()

    def hold():
        assert external._run_lock.acquire()
        entered.set()
        release.wait(5)
        external._run_lock.release()

    t = threading.Thread(target=hold)
    t.start()
    entered.wait(2)
    tool = fake_tool(tmp_path, {"scores": {}})
    try:
        assert external.run(tool, tmp_path, timeout=30).status == "busy"
    finally:
        release.set()
        t.join(2)


def test_job_spec_round_trip_and_unknown_policy_rejection():
    spec = search.JobSpec(policy=search.SEARCH_POLICY.to_dict(), seed=7, snapshot_key="local:abc")
    back = search.JobSpec.from_dict(spec.to_dict())
    assert back.seed == 7 and back.snapshot_key == "local:abc"
    with pytest.raises(SearchPolicyError):
        search.JobSpec.from_dict({"policy": {"id": "guided", "version": 999}, "seed": 1, "snapshot_key": "k"})
    with pytest.raises(SearchPolicyError):
        search.JobSpec.from_dict({"policy": search.SEARCH_POLICY.to_dict(), "seed": "x", "snapshot_key": "k"})


def test_p07_candidates_are_reverified_before_any_credit(tmp_path, monkeypatch):
    """P-07: upstream candidates are recompiled and re-verified through the coordinator; only the
    judge's verdict counts (a candidate that merely claims a good upstream score gets nothing)."""
    net = Network(tmp_path / "ext.db")
    make_project(net, 2)
    with net.store.tx() as db:
        db.execute("UPDATE functions SET fingerprint='fp-1', target=NULL WHERE id=1")
        db.execute("UPDATE functions SET fingerprint='fp-2', target=NULL WHERE id=2")
    alice = net.authenticate(net.register_agent("alice"))
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    ctx = net._context(proj["id"], fn["unit"])

    class FakeResult:
        ok, log, obj = True, "", b"ext"

    class FakeCand:
        def __init__(self, name, fp):
            self.name, self._fp = name, fp

        def fingerprint(self):
            return self._fp

    net._compile = lambda proj, header, source: FakeResult()
    net._evaluate = lambda fn, proj, obj: (FakeCand(fn["name"], fn["fingerprint"]), None, [])

    out = external.reverify(net, alice, fn, proj,
                            {"a": "int func_001(void) { return 1; }",
                             "b": "int func_001(void) { return 2; }"}, ctx)
    # both compile and judge as hash-only matches (fingerprint stub matches) -> accepted by the judge
    assert all(v["accepted"] for v in out.values())
    assert net.store.one("SELECT status FROM functions WHERE id=1")["status"] == "matched"

    # a candidate that doesn't compile gets nothing, and no candidate is credited without the judge
    net2 = Network(tmp_path / "ext2.db")
    make_project(net2, 2)
    with net2.store.tx() as db:
        db.execute("UPDATE functions SET fingerprint='fp-1', target=NULL WHERE id=1")
    alice2 = net2.authenticate(net2.register_agent("alice2"))
    fn2 = net2.store.one("SELECT * FROM functions WHERE id=1")
    proj2 = net2.store.one("SELECT * FROM projects WHERE id=1")
    ctx2 = net2._context(proj2["id"], fn2["unit"])

    class Broken:
        ok, log, obj = False, "nope", None

    net2._compile = lambda proj, header, source: Broken()
    out2 = external.reverify(net2, alice2, fn2, proj2, {"a": "int func_001(void) { return 1; }"}, ctx2)
    assert out2["a"]["why"] == "doesn't compile"
    assert net2.store.one("SELECT status FROM functions WHERE id=1")["status"] == "open"
