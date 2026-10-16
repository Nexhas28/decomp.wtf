"""Compilation cache and immutable include snapshots (plan P2.4-P2.8, tests C-01..C-12).

No real compiler is needed: `compiler._exec` is stubbed with an in-process runner that
inspects the staged inputs and writes a deterministic object file, so every test runs on
macOS, Windows and Linux alike.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from decompnet import cache as ccache
from decompnet import compiler
from decompnet.compiler import CompilerConfig, compile_source

SOURCE = "int add(int a, int b) { return a + b; }\n"
HEADER = "typedef int s32;\n"

EXE = sys.executable  # a real, readable file: a stable compiler identity for key building


def make_cfg(tmp_path: Path, *, sandbox: str = "none", **kw) -> CompilerConfig:
    cmd = kw.pop("cmd", [EXE, "-target", "fixture", "-I{include}", "{src}", "-o", "{obj}"])
    return CompilerConfig(
        cmd=cmd,
        sandbox=sandbox, cache_dir=str(tmp_path / "ccache"), **kw)


def key_of(cfg: CompilerConfig, header: str = HEADER, source: str = SOURCE) -> ccache.CompilationKey:
    return ccache.build_key(cfg, header, source, mode="none", work_base=cfg.cache_dir and Path(cfg.cache_dir) / "work")


class FakeRun:
    """Stubbed `_exec`: one call per producer run; deterministic object bytes."""

    def __init__(self, monkeypatch, *, fail: str = "", rc: int = 0, hold: threading.Event | None = None):
        self.calls: list[dict] = []
        self.fail = fail
        self.rc = rc
        self.hold = hold  # when set, the runner blocks until self.release is set
        self.release = threading.Event()
        monkeypatch.setattr(compiler, "_exec", self)

    def __call__(self, cfg, work, argv, timeout, cwd=None, *, snapshot=None, **stage):
        work = Path(work)
        self.calls.append({"work": work, "argv": argv, "snapshot": snapshot})
        if self.hold is not None:
            self.hold.set()
            self.release.wait(timeout + 120)
        out, err = "", self.fail
        if self.fail:
            return subprocess.CompletedProcess(argv, self.rc or 1, out, err)
        if self.rc == 0:
            (work / "func.o").write_bytes(b"OBJ:" + ccache.sha((work / "func.c").read_bytes()).encode())
        return subprocess.CompletedProcess(argv, self.rc, out, err)


def _wait_until(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not reached in time")
        time.sleep(0.005)


# ------------------------------------------------------------------ C-01 / C-06

def test_C01_six_identical_requests_run_one_compiler(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    run = FakeRun(monkeypatch, hold=threading.Event())
    results = [None] * 6
    errors: list[BaseException] = []

    def worker(i):
        try:
            results[i] = compile_source(cfg, HEADER, SOURCE)
        except BaseException as e:  # pragma: no cover - only on failure
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    assert run.hold.wait(10), "producer never started"
    cache = ccache.cache_for_dir(Path(cfg.cache_dir))
    _wait_until(lambda: cache.stats["coalesced"] == 5, 10)
    assert len(run.calls) == 1
    run.release.set()
    for t in threads:
        t.join(15)
    assert not errors
    assert all(r.ok for r in results)
    assert len({r.obj for r in results}) == 1  # all six share the same object bytes
    assert len(run.calls) == 1                 # and exactly one producer ran


def test_C06_single_flight_coalesces_concurrent_callers(tmp_path):
    c = ccache.CompileCache(tmp_path / "c")
    runs = []
    started = threading.Event()
    release = threading.Event()

    def producer():
        runs.append(1)
        started.set()
        release.wait(10)
        return "OBJ"

    results: list = []
    threads = [threading.Thread(target=lambda: results.append(c.get_or_run("k", producer)))
               for _ in range(3)]
    for t in threads:
        t.start()
    assert started.wait(10)
    _wait_until(lambda: c.stats["coalesced"] == 2, 10)
    assert len(runs) == 1
    release.set()
    for t in threads:
        t.join(15)
    assert results == ["OBJ"] * 3 and len(runs) == 1


# ------------------------------------------------------- C-02..C-05: key identity

def test_C02_incompatible_contexts_have_distinct_keys(tmp_path):
    base = make_cfg(tmp_path)
    k = key_of(base)
    others = [
        key_of(base, source=SOURCE + " "),
        key_of(base, header=HEADER + "\n"),
        key_of(make_cfg(tmp_path, cmd=[EXE, "-O2", "-I{include}", "{src}", "-o", "{obj}"]), source=SOURCE),
        key_of(make_cfg(tmp_path, timeout=7.5), source=SOURCE),
    ]
    assert len({k.digest, *(o.digest for o in others)}) == 1 + len(others)
    m = k.manifest
    assert m["cache_schema"] == ccache.CACHE_SCHEMA
    work = tmp_path / "ccache" / "work" / k.work_name
    assert m["compiler"]["argv"] == [EXE, "-target", "fixture", f"-I{work}/include",
                                     f"{work}/func.c", "-o", f"{work}/func.o"]
    assert m["compiler"]["exe"] == EXE and m["compiler"]["exe_sha"]
    assert m["path_semantics"]["staging"] == "deterministic"
    assert m["path_semantics"]["root"] == str(work)


def test_C03_key_is_exact_bytes_even_whitespace(tmp_path):
    cfg = make_cfg(tmp_path)
    sources = [
        "int f(void) { return 1; }",
        "int f(void) { return 1;}\n",
        "int  f(void) { return 1; }",
        "int f(void) { return 1; }  ",
        "int f(void) { return 1; }\t",
    ]
    digests = {key_of(cfg, source=s).digest for s in sources}
    assert len(digests) == len(sources)  # any byte change changes the key
    assert key_of(cfg).digest == key_of(cfg).digest  # identical bytes: identical key


def test_C04_include_define_and_order_changes_invalidate(tmp_path):
    d1 = tmp_path / "i1"
    d2 = tmp_path / "i2"
    d1.mkdir()
    d2.mkdir()
    (d1 / "a.h").write_text("#define A 1\n")
    (d2 / "a.h").write_text("#define A 2\n")
    (d2 / "b.h").write_text("int b;\n")
    cfg = make_cfg(tmp_path, include_dirs=(str(d1), str(d2)))
    base = key_of(cfg)
    assert key_of(cfg, header=HEADER + "\n").digest != base.digest                    # context header
    assert key_of(make_cfg(tmp_path, include_dirs=(str(d2), str(d1)))).digest != base.digest  # include order
    assert key_of(make_cfg(tmp_path, include_dirs=(str(d1),))).digest != base.digest          # dir removed
    (d1 / "a.h").write_text("#define A 9\n")
    assert key_of(cfg).digest != base.digest                                          # include content
    with_flags = make_cfg(tmp_path, cmd=[EXE, "-DX=1", "-I{include}", "{src}", "-o", "{obj}"])
    assert key_of(with_flags).digest != key_of(
        make_cfg(tmp_path, cmd=[EXE, "-DX=2", "-I{include}", "{src}", "-o", "{obj}"])).digest  # defines


def test_C05_compiler_flags_and_environment_change_the_key(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    base = key_of(cfg)
    # flags
    other = make_cfg(tmp_path, cmd=[EXE, "-O2", "-I{include}", "{src}", "-o", "{obj}"])
    assert key_of(other).digest != base.digest
    # compiler executable identity
    fake_cc = tmp_path / "fake-cc"
    fake_cc.write_bytes(b"#!/bin/sh\nexit 0\n")
    fake_cc.chmod(0o755)
    other2 = make_cfg(tmp_path, cmd=[str(fake_cc), "-I{include}", "{src}", "-o", "{obj}"])
    assert key_of(other2).digest != base.digest
    assert key_of(other2).manifest["compiler"]["exe"] == str(fake_cc)
    # relevant environment
    monkeypatch.setenv("CPATH", "/somewhere/else")
    assert key_of(cfg).digest != base.digest


# ----------------------------------------------------------------------- C-07

def test_C07_waiter_cancellation_is_isolated_and_last_exit_cancels_the_producer(tmp_path):
    c = ccache.CompileCache(tmp_path / "c")
    runs = []
    started = threading.Event()
    cancel_ev = threading.Event()
    release = threading.Event()

    def producer():
        runs.append(1)
        started.set()
        while not release.is_set():
            if cancel_ev.wait(0.05):
                raise ccache.CoalesceTimeout("cancelled")
        return "OBJ"

    outcomes: dict = {}
    threads = [
        threading.Thread(target=lambda: outcomes.setdefault("b", c.get_or_run("k", producer))),
        threading.Thread(target=lambda: outcomes.setdefault("c", c.get_or_run("k", producer))),
    ]
    for t in threads:
        t.start()
    assert started.wait(10)
    _wait_until(lambda: c.stats["coalesced"] == 1, 10)
    # one waiter cancels (times out) while the others keep waiting
    with pytest.raises(ccache.CoalesceTimeout):
        c.get_or_run("k", lambda: None, wait_timeout=0.05)
    assert len(runs) == 1 and "b" not in outcomes and "c" not in outcomes  # producer untouched
    release.set()
    for t in threads:
        t.join(15)
    assert outcomes["b"] == outcomes["c"] == "OBJ" and len(runs) == 1

    # every consumer detaches before the producer finishes: the producer gets a cancel
    c2 = ccache.CompileCache(tmp_path / "c2")
    started2 = threading.Event()
    cancelled = threading.Event()

    def producer2():
        started2.set()
        cancelled.wait(10)
        raise ccache.CoalesceTimeout("cancelled")

    def cancelled_worker():
        try:
            c2.get_or_run("k2", producer2, wait_timeout=0.05, cancel=cancelled.set)
        except ccache.CoalesceTimeout:
            pass

    threads2 = [threading.Thread(target=cancelled_worker) for _ in range(3)]
    for t in threads2:
        t.start()
    assert started2.wait(10)
    assert cancelled.wait(10), "producer was not cancelled after the last consumer left"
    for t in threads2:
        t.join(15)
    _wait_until(lambda: "k2" not in c2._flights, 10)
    assert c2.get("k2") is None  # a cancelled producer never publishes


# ----------------------------------------------------------------------- C-08

def test_C08_error_is_cached_negative_killed_is_transient(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    run = FakeRun(monkeypatch, fail="func.c:3: error: expected identifier", rc=1)
    r1 = compile_source(cfg, HEADER, SOURCE)
    assert not r1.ok and "error: expected identifier" in r1.log
    n = len(run.calls)
    r2 = compile_source(cfg, HEADER, SOURCE)
    assert not r2.ok and r2.log == r1.log
    assert len(run.calls) == n  # deterministic error: served from the cached negative
    cache = ccache.cache_for_dir(Path(cfg.cache_dir))
    assert cache.get(key_of(cfg).digest).outcome == ccache.OUTCOME_ERROR

    # killed compiler: transient -- no persistent entry, short in-process retry
    monkeypatch.setattr(run, "fail", "")
    monkeypatch.setattr(run, "rc", -9)
    src2 = SOURCE + " "
    k2 = key_of(cfg, source=src2).digest
    r3 = compile_source(cfg, HEADER, src2)
    assert not r3.ok and len(run.calls) == n + 1
    assert cache.get(k2) is None  # never stored
    r4 = compile_source(cfg, HEADER, src2)
    assert len(run.calls) == n + 1  # short retry policy: the transient result is reused
    assert not r4.ok
    cache.transient_retry = 0.0       # window expired: the compiler runs again
    r5 = compile_source(cfg, HEADER, src2)
    assert not r5.ok and len(run.calls) == n + 2
    assert cache.get(k2) is None      # still nothing cached


# ----------------------------------------------------------------------- C-09

def test_C09_tampered_payload_is_quarantined_and_recomputed(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    run = FakeRun(monkeypatch)
    assert compile_source(cfg, HEADER, SOURCE).ok
    assert len(run.calls) == 1
    key = key_of(cfg).digest
    obj_dir = tmp_path / "ccache" / "objects" / key
    (obj_dir / "payload").write_bytes(b"tampered")
    assert compile_source(cfg, HEADER, SOURCE).ok
    assert len(run.calls) == 2  # corrupt entry was recomputed, not served
    q = tmp_path / "ccache" / "quarantine" / key
    assert (q / "reason").read_text() == "payload hash mismatch"
    assert (q / "payload").read_bytes() == b"tampered"

    (obj_dir / "entry.json").write_text("{not json", encoding="utf-8")
    assert compile_source(cfg, HEADER, SOURCE).ok
    assert len(run.calls) == 3
    cache = ccache.cache_for_dir(Path(cfg.cache_dir))
    assert cache.get(key).payload is not None  # repaired entry verifies again


# ----------------------------------------------------------------------- C-10

def test_C10_rejected_source_is_rejected_before_cache_reuse(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    run = FakeRun(monkeypatch)
    assert compile_source(cfg, HEADER, SOURCE).ok  # cached under today's policy
    monkeypatch.setattr(compiler, "check_source",
                        lambda s: (_ for _ in ()).throw(compiler.SourceRejected("new policy")))
    r = compile_source(cfg, HEADER, SOURCE)
    assert not r.ok and r.log == "rejected: new policy"
    assert len(run.calls) == 1  # policy first: the cache was never consulted
    r2 = compile_source(cfg, HEADER, SOURCE, trusted=True)  # trusted skips policy, hits the cache
    assert r2.ok and len(run.calls) == 1


# ----------------------------------------------------------------------- C-11

def test_C11_snapshots_are_read_only_shared_and_isolated(tmp_path, monkeypatch):
    incdir = tmp_path / "incdir"
    incdir.mkdir()
    (incdir / "defs.h").write_text("#define K 3\n")
    cfg = make_cfg(tmp_path, include_dirs=(str(incdir),))
    run = FakeRun(monkeypatch)
    assert compile_source(cfg, HEADER, SOURCE).ok
    work = run.calls[0]["work"]
    inc = work / "include"
    assert (inc / "context.h").read_text(encoding="utf-8") == HEADER
    assert (inc / "defs.h").read_text(encoding="utf-8") == "#define K 3\n"

    snaps = list((tmp_path / "ccache" / "snapshots").iterdir())
    assert len(snaps) == 1
    snap_inc = snaps[0] / "include"
    if os.geteuid() != 0:
        for p in snap_inc.rglob("*"):
            assert stat.S_IMODE(p.lstat().st_mode) & 0o222 == 0, f"{p} is writable"
        with pytest.raises(PermissionError):
            os.open(snap_inc / "context.h", os.O_WRONLY)

    # the attempt gets its own copy -- never a writable hard link to the shared snapshot
    assert os.stat(inc / "context.h").st_ino != os.stat(snap_inc / "context.h").st_ino
    (inc / "context.h").chmod(0o644)
    (inc / "context.h").write_text("mutated by the attempt", encoding="utf-8")
    assert (snap_inc / "context.h").read_text(encoding="utf-8") == HEADER

    # the same dependency revision reuses the same snapshot
    done_mtime = (snaps[0] / "DONE").stat().st_mtime_ns
    assert compile_source(cfg, HEADER, SOURCE + " ").ok
    assert list((tmp_path / "ccache" / "snapshots").iterdir()) == snaps
    assert (snaps[0] / "DONE").stat().st_mtime_ns == done_mtime


# ----------------------------------------------------------------------- C-12

def test_C12_quota_eviction_never_deletes_in_use_payloads(tmp_path):
    c = ccache.CompileCache(tmp_path / "q", max_bytes=1300, max_age=None)
    c.put("a", ok=True, outcome=ccache.OUTCOME_SUCCESS, payload=b"A" * 100, log="")
    c.pin("a")  # "a" is in use
    c.put("b", ok=True, outcome=ccache.OUTCOME_SUCCESS, payload=b"B" * 100, log="")
    c.put("c", ok=True, outcome=ccache.OUTCOME_SUCCESS, payload=b"C" * 100, log="")  # over quota
    assert c.get("a") is not None and c.get("a").payload == b"A" * 100  # pinned: kept
    assert c.get("b") is None                                          # oldest unpinned: evicted
    assert c.get("c") is not None
    c.unpin("a")
    c.put("d", ok=True, outcome=ccache.OUTCOME_SUCCESS, payload=b"D" * 100, log="")  # over quota again
    assert c.get("a") is None and c.get("d") is not None

    c2 = ccache.CompileCache(tmp_path / "q2", max_bytes=None, max_age=3600)
    c2.put("old", ok=True, outcome=ccache.OUTCOME_SUCCESS, payload=b"x", log="")
    c2.pin("kept")
    c2.put("kept", ok=True, outcome=ccache.OUTCOME_SUCCESS, payload=b"y", log="")
    assert c2.evict(now=time.time() + 7200) >= 1
    assert c2.get("old") is None and c2.get("kept") is not None       # age eviction skips in-use


# ------------------------------------------------ P2.8: diagnostics and snapshot quota

def test_miss_reasons_new_input_vs_changed_dependencies(tmp_path):
    from decompnet import telemetry

    cfg = make_cfg(tmp_path)
    k_a = key_of(cfg)
    k_b = key_of(cfg, header=HEADER + "\n")      # same request, different context header
    k_c = key_of(cfg, source=SOURCE + " ")       # different request
    assert k_a.digest != k_b.digest
    assert k_a.request_id == k_b.request_id != k_c.request_id

    c = ccache.CompileCache(tmp_path / "c")
    was_enabled = telemetry.enabled()
    telemetry.enable(True)
    telemetry.clear()
    try:
        assert c.get(k_a.digest, request=k_a.request_id) is None      # first sight: new input
        c.put(k_a.digest, ok=True, outcome=ccache.OUTCOME_SUCCESS, payload=b"OBJ", log="",
              manifest=k_a.manifest)
        assert c.get(k_a.digest, request=k_a.request_id).payload == b"OBJ"
        assert c.get(k_b.digest, request=k_b.request_id) is None      # same request, new dependencies
        assert c.get(k_c.digest, request=k_c.request_id) is None      # new input
        counters = telemetry.snapshot()["counters"]
    finally:
        telemetry.clear()
        telemetry.enable(was_enabled)
    assert counters == {"cache.hit": 1, "cache.miss.new-input": 2,
                        "cache.miss.dependency-changed": 1}
    assert c.stats["hits"] == 1 and c.stats["misses"] == 3
    assert c.stats["misses_new"] == 2 and c.stats["misses_dependency_changed"] == 1
    text = c.diagnose()
    assert "2 new-input" in text and "1 dependency-changed" in text and "bytes" in text


def test_snapshots_count_against_the_size_quota(tmp_path):
    inc = tmp_path / "inc"
    inc.mkdir()
    (inc / "big.h").write_bytes(b"/" * 4000)
    key = key_of(make_cfg(tmp_path, include_dirs=(str(inc),)))
    c = ccache.CompileCache(tmp_path / "c", max_bytes=3000, max_age=None)
    c.ensure_snapshot(key)
    snap = tmp_path / "c" / "snapshots" / key.dep_digest
    assert (snap / "DONE").exists()

    c.pin_snapshot(key.dep_digest)   # in use: never evicted, even over quota
    assert c.evict() == 0 and snap.exists()
    c.unpin_snapshot(key.dep_digest)
    assert c.evict() == 1 and not snap.exists()   # read-only tree is removed for real

    # objects and snapshots share one quota; the oldest unpinned item goes first
    c2 = ccache.CompileCache(tmp_path / "c2", max_bytes=5000, max_age=None)
    c2.ensure_snapshot(key)
    snap2 = tmp_path / "c2" / "snapshots" / key.dep_digest
    old = time.time() - 100
    os.utime(snap2 / "DONE", (old, old))
    c2.put("a", ok=True, outcome=ccache.OUTCOME_SUCCESS, payload=b"A" * 600, log="")
    assert not snap2.exists()                    # snapshot (oldest) evicted to fit the quota
    assert c2.get("a") is not None


def test_in_flight_is_true_only_while_the_producer_runs(tmp_path):
    c = ccache.CompileCache(tmp_path / "c")
    started, release = threading.Event(), threading.Event()

    def producer():
        started.set()
        release.wait(10)
        return "OBJ"

    t = threading.Thread(target=lambda: c.get_or_run("k", producer))
    t.start()
    assert started.wait(10)
    assert c.in_flight("k") and not c.in_flight("other")
    release.set()
    t.join(15)
    _wait_until(lambda: not c.in_flight("k"), 10)


def test_waiter_timeout_while_producer_runs_does_not_start_a_second_compile(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    run = FakeRun(monkeypatch, hold=threading.Event())
    cache = ccache.cache_for_dir(Path(cfg.cache_dir))
    digest = key_of(cfg).digest
    first: list = []
    producer = threading.Thread(target=lambda: first.append(compile_source(cfg, HEADER, SOURCE)))
    producer.start()
    assert run.hold.wait(10), "producer never started"
    assert cache.in_flight(digest)

    real = ccache.CompileCache.get_or_run
    monkeypatch.setattr(ccache.CompileCache, "get_or_run",
                        lambda self, key, producer, **kw: real(self, key, producer, **{**kw, "wait_timeout": 0.05}))
    waiter = compile_source(cfg, HEADER, SOURCE)
    assert not waiter.ok and waiter.obj is None and waiter.log.startswith("timeout")
    assert len(run.calls) == 1                       # no second compiler execution
    assert cache.transient_get(digest) is None       # the waiter's timeout is not recorded ...
    assert cache.get(digest) is None                 # ... or cached as a failure

    run.release.set()
    producer.join(15)
    assert first and first[0].ok
    assert len(run.calls) == 1
    assert cache.get(digest).outcome == ccache.OUTCOME_SUCCESS


# ---------------------------------------------------------- bypass / rollback knobs

def test_bypass_when_the_key_cannot_be_described(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, cmd=["no-such-compiler-xyz", "-I{include}", "{src}", "-o", "{obj}"])
    run = FakeRun(monkeypatch)
    assert compile_source(cfg, HEADER, SOURCE).ok
    assert compile_source(cfg, HEADER, SOURCE).ok
    assert len(run.calls) == 2  # uncached legacy path: one compile per request
    objs = tmp_path / "ccache" / "objects"
    assert not objs.exists() or not any(objs.iterdir())


def test_wsl_and_docker_modes_bypass_the_cache(tmp_path):
    assert ccache.build_key(make_cfg(tmp_path, wsl=True), HEADER, SOURCE,
                            mode="wsl", work_base=tmp_path) is None
    assert ccache.build_key(make_cfg(tmp_path, sandbox="docker", image="img"), HEADER, SOURCE,
                            mode="docker", work_base=tmp_path) is None


def test_cache_can_be_disabled_per_config_and_per_env(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, cache=False)
    run = FakeRun(monkeypatch)
    compile_source(cfg, HEADER, SOURCE)
    compile_source(cfg, HEADER, SOURCE)
    assert len(run.calls) == 2  # disabled: no reuse
    monkeypatch.setenv("DECOMPNET_COMPILE_CACHE", "0")
    cfg2 = make_cfg(tmp_path)
    compile_source(cfg2, HEADER, SOURCE)
    assert len(run.calls) == 3
