"""Stage-level telemetry (plan P0.3 / PR-02).

Monotonic-clock durations, UTC timestamps, per-stage ring buffer with cache hit/miss reasons, failed
compilation classification, source origin and verification level. Disabled by default: with
telemetry off, the instrumentation is a no-op (a single env check per stage) so the hot path keeps
its baseline behaviour byte-for-byte. Enable with DECOMP_TELEMETRY=on (or telemetry.enable()).

The recorder is thread-local: every stage is attributed to the worker that ran it, and aggregate
worker time is reported next to wall time (plan P0.3).
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from contextlib import contextmanager

_ENABLED = threading.Event()
if os.environ.get("DECOMP_TELEMETRY") == "on":
    _ENABLED.set()

_local = threading.local()
MAX_STAGES = 2000  # ring buffer per thread


def enabled() -> bool:
    return _ENABLED.is_set()


def enable(on: bool = True) -> None:
    _ENABLED.set() if on else _ENABLED.clear()


class _Recorder:
    def __init__(self):
        self.stages: deque = deque(maxlen=MAX_STAGES)
        self.counters: dict = {}
        self.started = time.monotonic()

    def add(self, name: str, seconds: float, **fields):
        entry = {"name": name, "ms": round(1000 * seconds, 3),
                 "at_utc": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())}
        entry.update(fields)
        self.stages.append(entry)

    def count(self, name: str, delta: int = 1):
        self.counters[name] = self.counters.get(name, 0) + delta


def _rec() -> _Recorder | None:
    if not enabled():
        return None
    r = getattr(_local, "recorder", None)
    if r is None:
        r = _local.recorder = _Recorder()
    return r


@contextmanager
def span(name: str, **fields):
    """One timed stage; fields (cache hit/miss, origin, verification level, ...) are attached on exit."""
    r = _rec()
    started = time.monotonic() if r is not None else 0.0
    try:
        yield r
    finally:
        if r is not None:
            r.add(name, time.monotonic() - started, **fields)


def count(name: str, delta: int = 1) -> None:
    r = _rec()
    if r is not None:
        r.count(name, delta)


def snapshot() -> dict:
    """Aggregate telemetry across the current thread's stages and counters."""
    r = _rec()
    if r is None:
        return {"enabled": False}
    stages = list(r.stages)
    wall_ms = 1000 * (time.monotonic() - r.started)
    by_name: dict = {}
    for s in stages:
        by_name.setdefault(s["name"], []).append(s["ms"])
    return {
        "enabled": True,
        "wall_ms": round(wall_ms, 3),
        "worker_ms": round(sum(s["ms"] for s in stages), 3),
        "stage_count": len(stages),
        "stage_ms": {name: {"n": len(v), "total_ms": round(sum(v), 3), "max_ms": round(max(v), 3)}
                     for name, v in by_name.items()},
        "counters": dict(r.counters),
    }


def clear() -> None:
    r = _rec()
    if r is not None:
        r.stages.clear()
        r.counters.clear()
        r.started = time.monotonic()


def to_json() -> str:
    return json.dumps(snapshot(), indent=2)
