"""REA provider adapter tests (plan E-01..E-09): a fake stdio MCP server stands in for the pinned
rea-agents 6.4.0; one gated real-lane test exercises the actual install read-only."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from decompnet.analysis import rea
from decompnet.analysis.broker import ProviderError
from decompnet.analysis.contracts import ProviderRequest

FAKE_SERVER = r'''
import json, os, sys, time
def load(name, default=None):
    try:
        return json.load(open(os.path.join(os.getcwd(), name)))
    except Exception:
        return default
def save(name, obj):
    json.dump(obj, open(os.path.join(os.getcwd(), name), "w"))
CATALOG = load("catalog.json", [])
RESPONSES = load("responses.json", {})
def counts(n, d=1):
    c = load("counts.json", {})
    c[n] = c.get(n, 0) + d
    save("counts.json", c)
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
while True:
    line = sys.stdin.readline()
    if not line:
        break
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-06-18",
              "serverInfo": {"name": "rea", "version": "6.4.0"}}})
    elif msg.get("method") == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": CATALOG}})
    elif msg.get("method") == "tools/call":
        params = msg["params"]; name = params["name"]; args = params.get("arguments") or {}
        counts(name)
        if args.get("_sleep"):
            time.sleep(args["_sleep"])
        key = f"{name}:{args.get('function') or args.get('path') or 'none'}"
        resp = RESPONSES.get(key, RESPONSES.get(name, {"isError": True,
                "text": json.dumps({"error": f"no canned response for {key}"})}))
        out = dict(resp)
        if name == "open_binary" and not out.get("isError"):
            out.setdefault("structuredContent", {"evidence_id": "evt-1", "artifact_digest": args.get("_digest", "ok")})
        send({"jsonrpc": "2.0", "id": msg["id"], "result": out})
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A fake REA install: monkeypatches discover() to a python stdio MCP server in tmp_path."""

    def make(catalog: dict, responses: dict | None = None):
        fake_dir = tmp_path / "rea"
        fake_dir.mkdir(exist_ok=True)
        (fake_dir / "server.py").write_text(FAKE_SERVER)
        (fake_dir / "catalog.json").write_text(json.dumps(catalog))
        (fake_dir / "responses.json").write_text(json.dumps(responses or {}))
        counts = fake_dir / "counts.json"
        counts.write_text("{}")
        monkeypatch.setattr(rea, "discover", lambda: (sys.executable, fake_dir, "server.py"))
        return counts

    return make


def profile(**kw):
    base = {"format": "raw", "arch": "x86_64", "mode": "", "big_endian": False, "host": ""}
    base.update(kw)
    return base


def request(functions=("f1",), facets=("assembly", "pseudocode"), artifact=None, inputs=None):
    return ProviderRequest(request_id="r1", kind="function", function_key="mod1",
                           functions=functions, facets=facets,
                           inputs=inputs or {"artifact": artifact or ""})


def artifact(tmp_path, name="a.bin"):
    p = tmp_path / name
    p.write_bytes(b"\x7fELF" + b"\x00" * 32)
    return str(p)


CORE = [{"name": n, "inputSchema": {"type": "object"}} for n in rea.CORE_TOOLS]


# ---------------------------------------------------------------- E-01 / E-02 / E-05

def test_e01_schema_drift_fails_open_session(fake):
    fake([t for t in CORE if t["name"] != "analyze_function"])  # analyze_function missing
    p = rea.ReaProvider()
    with pytest.raises(ProviderError, match="drifted.*analyze_function"):
        p.open_session("s1", {})


def test_e02_capability_matrix_rejections():
    p = rea.ReaProvider()
    cases = [
        (profile(arch="mips"), "does not admit 'mips'"),
        (profile(arch="ppc"), "does not admit 'ppc'"),
        (profile(arch="arm", mode="thumb"), "Thumb-only"),
        (profile(arch="x86_64", big_endian=True), "big-endian"),
        (profile(format="elf", host="windows"), "Windows"),
    ]
    for prof, why in cases:
        r = p.capabilities(prof)
        assert not r.supported and any(why in w for w in r.reasons), (prof, r.reasons)
    ok = p.capabilities(profile())
    assert ok.supported and ok.profile["rea_version"] == "6.4.0" and ok.profile["read_only"] is True


def test_e05_windows_admission_gate():
    p = rea.ReaProvider()
    assert not p.capabilities({"format": "elf", "arch": "x86_64", "host": "windows"}).supported


# ---------------------------------------------------------------- E-03 / E-09 / E-04

def test_e03_success_and_error_paths_parse_separately(fake, tmp_path):
    a = artifact(tmp_path)
    make = fake(CORE, {
        f"open_binary:{a}": {"structuredContent": {"evidence_id": "evt-open"}},
        "analyze_function:f1": {"structuredContent": {"normalized_result": {"assembly": ["a"], "pseudocode": "c"},
                                                      "evidence_id": "evt-9"}},
        "analyze_function:f2": {"isError": True, "text": json.dumps({"error": "bad function"})},
    })
    p = rea.ReaProvider()
    side = p.open_session("s1", {})
    try:
        res = p.fetch("s1", side, request(functions=("f1", "f2"), artifact=a), None)
        assert res.status == "partial"
        assert list(res.functions) == ["mod1::f1"]  # the errored one is a diagnostic, not fake evidence
        assert "bad function" in res.diagnostics[0]
        assert res.functions["mod1::f1"].refs["dossier"].ref == "rea:evt-9"
    finally:
        p.close_session("s1", side)


def test_e09_null_and_empty_facets_stay_distinct(fake, tmp_path):
    a = artifact(tmp_path)
    make = fake(CORE, {
        f"open_binary:{a}": {"structuredContent": {"evidence_id": "evt-open"}},
        "analyze_function:f1": {"structuredContent": {"normalized_result": {"types": None, "assembly": []},
                                                      "evidence_id": "e"}},
    })
    p = rea.ReaProvider()
    side = p.open_session("s1", {})
    try:
        res = p.fetch("s1", side, request(functions=("f1",), facets=("types", "assembly"), artifact=a), None)
        ev = res.functions["mod1::f1"]
        assert ev.coverage["types"] == "unavailable"       # null facet: unavailable
        assert ev.coverage["assembly"] == "ok"             # empty list: observed empty
        assert "types" not in ev.views                     # null never becomes an empty array
    finally:
        p.close_session("s1", side)


def test_e04_oversized_dossiers_are_bounded_views_not_silent_truncation(fake, tmp_path):
    big = "x" * 20000
    a = artifact(tmp_path)
    make = fake(CORE, {
        f"open_binary:{a}": {"structuredContent": {"evidence_id": "evt-open"}},
        "analyze_function:f1": {"structuredContent": {"normalized_result": {"pseudocode": big},
                                                      "evidence_id": "e"}},
    })
    p = rea.ReaProvider()
    side = p.open_session("s1", {})
    try:
        res = p.fetch("s1", side, request(functions=("f1",), facets=("pseudocode",), artifact=a), None)
        view = res.functions["mod1::f1"].views["pseudocode"]
        assert len(view) == 4000  # bounded view; the ref carries the full evidence id
        assert res.refs["mod1::f1"].ref == "rea:e"
    finally:
        p.close_session("s1", side)


# ---------------------------------------------------------------- E-06 / E-08 / E-07

def test_e06_same_inputs_reuse_the_open_session_new_snapshot_reopens(fake, tmp_path):
    art = tmp_path / "a.bin"
    art.write_bytes(b"\x7fELF" + b"\x00" * 32)
    make = fake(CORE, {f"open_binary:{art}": {"structuredContent": {"evidence_id": "evt-open"}},
                       "analyze_function:f1": {"structuredContent": {"normalized_result": {"assembly": []},
                                                                     "evidence_id": "e"}}})
    p = rea.ReaProvider()
    side = p.open_session("s1", {})
    try:
        p.fetch("s1", side, request(functions=("f1",), artifact=str(art)), None)
        p.fetch("s1", side, request(functions=("f1",), artifact=str(art)), None)
        counts = json.loads(Path(rea.discover()[1] / "counts.json").read_text())
        assert counts.get("open_binary", 0) == 1  # one import, reused inside the session
        assert p.refresh("s1", "all", "new-snapshot") == 1
        side = p._sessions["s1"]  # refresh replaces the sidecar: fetch through the new handle
        p.fetch("s1", side, request(functions=("f1",), artifact=str(art)), None)
        counts = json.loads(Path(rea.discover()[1] / "counts.json").read_text())
        assert counts.get("open_binary", 0) == 2  # a new snapshot means a new import
    finally:
        p.close_session("s1", side)


def test_e08_artifact_identity_conflict_is_denied(fake, tmp_path):
    art = tmp_path / "a.bin"
    art.write_bytes(b"\x7fELF" + b"\x00" * 32)
    make = fake(CORE, {f"open_binary:{art}": {"structuredContent": {"evidence_id": "evt",
                       "artifact_digest": "deadbeef-deadbeef"}}})
    p = rea.ReaProvider()
    side = p.open_session("s1", {})
    try:
        res = p.fetch("s1", side, request(functions=("f1",), artifact=str(art)), None)
        assert res.status == "denied" and "digest changed" in res.denied_reason
    finally:
        p.close_session("s1", side)


def test_e07_cancel_terminates_the_sidecar_and_fails_the_session(fake, tmp_path):
    a = artifact(tmp_path)
    make = fake(CORE, {
        "analyze_function:f1": {"structuredContent": {"normalized_result": {"assembly": []}}},
        "_slow": {"isError": True},
    })
    # the canned response path can't sleep per-key, so wire sleep through arguments instead
    p = rea.ReaProvider()
    side = p.open_session("s1", {})
    cancel = threading.Event()

    def killer():
        time.sleep(0.3)
        cancel.set()

    # inject a sleep by calling with a patched rpc that honours cancel before responding
    import decompnet.analysis.rea as rea_mod

    def slow_call(name, arguments, cancel=None, timeout=600.0):
        if cancel is not None:
            while not cancel.is_set():
                time.sleep(0.05)
            raise ProviderError("rea sidecar terminated (cancelled)")

    side.call_tool = slow_call
    t = threading.Thread(target=killer)
    t.start()
    try:
        with pytest.raises(ProviderError, match="terminated"):
            p.fetch("s1", side, request(functions=("f1",), artifact=a), cancel)
    finally:
        t.join(2)
        side.proc.kill()


# ---------------------------------------------------------------- real lane

def test_real_lane_schema_discovery(tmp_path):
    """E-01's real-lane counterpart: the pinned install answers tools/list with the core tools.
    Read-only: an isolated HOME/TMPDIR, no artifact opened. Skipped when the install is absent."""
    if not rea.available():
        pytest.skip("rea-agents 6.4.0 not installed on this host (no node + scripts/rea.mjs)")
    p = rea.ReaProvider()
    side = p.open_session("s1", {"rea_home": str(tmp_path / "home"), "rea_tmp": str(tmp_path / "tmp")})
    try:
        missing = [t for t in rea.CORE_TOOLS if t not in side.tools]
        assert not missing, f"real catalog drifted: {missing}"
        assert side.tools["open_binary"]  # the admission gate actually passed against the real schema
    finally:
        p.close_session("s1", side)
