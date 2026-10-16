"""Adaptive scheduling (plan P11.1-P11.7): an explainable, deterministic priority policy on top of
the existing claim strategies, plus a global resource budget so optional workers cannot oversubscribe
the host.

The policy (P11.2) keeps the existing virtues (twins first, small functions, less-abandoned work,
files nearer completion) and adds waiting-age fairness, cost and no-progress penalties. Every score
comes with the reasoning behind it; weights are versioned so an experiment identity can pin them
(P11.6/W-06). The policy is advisory for ordering: every claim and every acceptance still goes
through the existing lease/judge paths unchanged.
"""

from __future__ import annotations

import math
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

PRIORITY_VERSION = "priority-v1"
WEIGHTS = {
    "progress": 1.0,        # how close the current best already is (expected verified progress)
    "twin": 0.5,            # bounded twin/unit-completion benefit
    "unit": 0.3,            # files closer to being fully linked first
    "fairness": 0.2,        # waiting-age credit (eventual service for harder work)
    "cost": 0.15,           # expected compile/analysis/agent cost (attempts already spent)
    "no_progress": 0.3,     # repeated-no-progress penalty
}


def _safe(n: int | None, cap: int) -> float:
    return max(0.0, min(n or 0, cap)) / cap


def components(net, fn, proj) -> dict:
    """The policy components for one function (P11.2), each with its weight and value."""
    n_insns = max(fn["n_insns"] or 0, 1)
    best = fn["best_score"]
    if best is None:
        progress = 0.0  # unknown closeness: no expected-progress signal either way
    else:
        progress = 1.0 - min(max(best, 0) / (n_insns * 100), 1.0)
    twin = min(max(fn["boost"] or 0, 0), 4) / 4
    unit = 0.0
    if fn["unit"]:
        row = net.store.one(
            "SELECT COUNT(*) AS total, SUM(status='matched') AS matched FROM functions "
            "WHERE project_id=? AND unit=? AND status IN ('open','matched')", proj["id"], fn["unit"])
        if row and row["total"]:
            unit = (row["matched"] or 0) / row["total"]
    first = net.store.one("SELECT MIN(created_at) AS t FROM attempts WHERE function_id=?", fn["id"])
    age_hours = max(time.time() - (first["t"] or time.time()), 0) / 3600 if first else 0.0
    fairness = min(math.log1p(age_hours) / math.log1p(72), 1.0)  # saturates at ~3 days of waiting
    attempts = fn["attempts"] or 0
    cost = _safe(attempts, 40)
    no_progress = 0.0
    if best is not None:
        rows = net.store.all("SELECT score FROM attempts WHERE function_id=? ORDER BY id DESC LIMIT 24", fn["id"])
        streak, best_seen = 0, None
        for r in reversed(rows):  # attempts since the last improvement (the same measure as stall_note)
            if r["score"] is not None and (best_seen is None or r["score"] < best_seen):
                best_seen, streak = r["score"], 0
            else:
                streak += 1
        no_progress = _safe(streak, 24)
    return {
        "progress": {"weight": WEIGHTS["progress"], "value": progress, "why": f"best {best or 'n/a'} of {n_insns} insns"},
        "twin": {"weight": WEIGHTS["twin"], "value": twin, "why": f"boost {fn['boost'] or 0}"},
        "unit": {"weight": WEIGHTS["unit"], "value": unit, "why": f"unit {fn['unit'] or '(project)'} completion"},
        "fairness": {"weight": WEIGHTS["fairness"], "value": fairness, "why": f"waiting {age_hours:.1f}h"},
        "cost": {"weight": WEIGHTS["cost"], "value": -cost, "why": f"{attempts} attempts spent"},
        "no_progress": {"weight": WEIGHTS["no_progress"], "value": -no_progress, "why": "no better in recent window"},
    }


def priority(net, fn, proj) -> float:
    return sum(c["weight"] * c["value"] for c in components(net, fn, proj).values())


def explain(net, fn, proj) -> dict:
    comps = components(net, fn, proj)
    return {"version": PRIORITY_VERSION, "function": fn["name"], "priority": round(priority(net, fn, proj), 4),
            "components": comps}


def choose(net, proj, candidates: list, limit: int = 1) -> list:
    """Pick the best candidates under the policy; deterministic (priority, then id)."""
    ranked = sorted(candidates, key=lambda f: (priority(net, f, proj), -f["id"]), reverse=True)
    return ranked[:limit]


@dataclass
class ResourceBudget:
    """P11.6: one global account for optional workers so separately-tuned pools cannot oversubscribe
    the host. Pools are counters with hard limits; `acquire` fails fast instead of queuing unbounded
    work, while `slot` waits (optionally with a timeout) for a free unit so the real call sites, the
    compiler first of all, are held to the same account."""

    limits: dict
    _used: dict
    _waiting: dict
    _lock: threading.Lock
    _cond: threading.Condition

    def __init__(self, limits: dict | None = None):
        self.limits = dict(limits or {"compiler": max(2, (os.cpu_count() or 4) - 1), "permuter": 1,
                                      "external": 1, "analysis": 1, "fork_build": 2})
        self._used = {k: 0 for k in self.limits}
        self._waiting = {k: 0 for k in self.limits}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

    def acquire(self, pool: str) -> bool:
        with self._lock:
            if pool not in self.limits:
                raise KeyError(f"unknown resource pool {pool!r}")
            if self._used[pool] >= self.limits[pool]:
                return False
            self._used[pool] += 1
            return True

    def release(self, pool: str) -> None:
        with self._lock:
            if self._used.get(pool, 0) > 0:
                self._used[pool] -= 1
                self._cond.notify_all()

    @contextmanager
    def slot(self, pool: str, timeout: float | None = None):
        """Hold one unit of `pool` for the body, waiting for a free unit first. Raises TimeoutError
        if none frees up within `timeout` seconds (None waits indefinitely, 0 never waits)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            if pool not in self.limits:
                raise KeyError(f"unknown resource pool {pool!r}")
            if self._used[pool] >= self.limits[pool]:
                self._waiting[pool] += 1
                try:
                    while self._used[pool] >= self.limits[pool]:
                        remaining = None if deadline is None else deadline - time.monotonic()
                        if remaining is not None and remaining <= 0:
                            raise TimeoutError(f"{pool} budget: no free slot within {timeout}s")
                        self._cond.wait(remaining)
                finally:
                    self._waiting[pool] -= 1
            self._used[pool] += 1
        try:
            yield
        finally:
            self.release(pool)

    def pressure(self) -> dict:
        with self._lock:
            return {"limits": dict(self.limits), "used": dict(self._used), "waiting": dict(self._waiting)}

    def acquire_context(self, pool: str):
        class _Guard:
            def __init__(self, budget, pool):
                self.budget, self.pool = budget, pool

            def __enter__(self):
                if not self.budget.acquire(self.pool):
                    raise RuntimeError(f"{self.pool} budget exhausted")
                return self

            def __exit__(self, *exc):
                self.budget.release(self.pool)
                return False

        return _Guard(self, pool)


BUDGET = ResourceBudget()
