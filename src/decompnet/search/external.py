"""Optional upstream decomp-permuter adapter (plan P5.7, PR-19).

The upstream tool (https://github.com/simonlindholm/decomp-permuter) is NOT vendored and NOT
required: when it is absent the internal guided search (PR-18) runs, as always.

- Discovery is explicit (a configured path, or PATH). Absent => `available()` is False.
- Runs are snapshot-specific and bounded: one external run at a time process-wide (no nested
  oversubscription, plan P5.7 step 6), with a hard timeout.
- Upstream scores are ADVISORY (their scale differs, plan P5.7 step 4): every returned candidate is
  recompiled and re-verified through the coordinator before it can improve anything (step 5); the
  judge in `network._judge` is the only acceptance path.
- Provenance/notices (step 7): this adapter is original code; nothing from upstream is vendored. If
  an upstream operator is ever copied in, its license notice must be shipped alongside.

Job identity for distributed use (P5.8 / P-08): `JobSpec` carries the versioned search policy
(P5.8), the seed and the snapshot key; a worker that cannot resolve the policy must reject the job
(`SearchPolicyError`), never resubmit guesses.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .policy import SEARCH_POLICY, SearchPolicy, SearchPolicyError, parse_policy

UPSTREAM = {
    "repo": "https://github.com/simonlindholm/decomp-permuter",
    "revision": "pinned-at-adoption (record the exact commit in deployment manifests; see plan P5.7 step 1)",
    "notices": "no upstream code vendored; the adapter only drives the CLI contract",
}

# One external run at a time process-wide (P5.7 step 6): the internal permuter already uses spare
# CPU, and a second upstream run would oversubscribe the host.
_run_lock = threading.Lock()


@dataclass
class ExternalRun:
    status: str  # ok | busy | timeout | error | unavailable
    scores: dict = field(default_factory=dict)      # name -> advisory score (upstream scale)
    candidates: dict = field(default_factory=dict)  # name -> proposed source text
    log: str = ""
    seconds: float = 0.0


@dataclass
class JobSpec:
    """The versioned spec a distributed search job carries (P5.8 / P-08)."""

    policy: dict
    seed: int
    snapshot_key: str

    def to_dict(self) -> dict:
        return {"policy": self.policy, "seed": self.seed, "snapshot_key": self.snapshot_key}

    @staticmethod
    def from_dict(d: dict) -> "JobSpec":
        if not isinstance(d, dict):
            raise SearchPolicyError("job spec must be an object")
        policy = parse_policy(d.get("policy") or {})  # unknown policy versions raise: reject, don't guess
        seed = d.get("seed")
        if not isinstance(seed, int):
            raise SearchPolicyError("job spec needs an integer seed")
        snapshot_key = d.get("snapshot_key")
        if not isinstance(snapshot_key, str) or not snapshot_key:
            raise SearchPolicyError("job spec needs a snapshot_key string")
        return JobSpec(policy=policy.to_dict(), seed=seed, snapshot_key=snapshot_key)


def discover(config_path: str | None = None) -> str | None:
    """The upstream binary, from an explicit config path or PATH. None: not available."""
    if config_path:
        p = os.path.expanduser(config_path)
        return p if os.path.isfile(p) and os.access(p, os.X_OK) else None
    return shutil.which("decomp-permuter") or shutil.which("permuter")


def available(config_path: str | None = None) -> bool:
    return discover(config_path) is not None


def run(exe: str, workdir: Path, *, timeout: float = 300) -> ExternalRun:
    """One bounded upstream run in `workdir`. The tool must write scores.json with
    {"scores": {name: score}, "candidates": {name: source}}; the wrapper script this module's
    caller writes into workdir is responsible for producing that contract."""
    started = time.monotonic()
    if not _run_lock.acquire(blocking=False):
        return ExternalRun(status="busy", log="another external permuter run is active")
    try:
        try:
            proc = subprocess.run([exe], cwd=workdir, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return ExternalRun(status="timeout", seconds=time.monotonic() - started,
                               log=f"exceeded {timeout:.0f}s")
        log = (proc.stdout or "")[-4000:] + (proc.stderr or "")[-2000:]
        if proc.returncode != 0:
            return ExternalRun(status="error", log=log, seconds=time.monotonic() - started)
        try:
            payload = json.loads((Path(workdir) / "scores.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return ExternalRun(status="error", log=f"bad scores.json: {e}", seconds=time.monotonic() - started)
        if not isinstance(payload, dict) or not isinstance(payload.get("scores"), dict):
            return ExternalRun(status="error", log="scores.json needs a scores object", seconds=time.monotonic() - started)
        return ExternalRun(status="ok", scores=payload["scores"],
                           candidates=payload.get("candidates") or {},
                           log=log, seconds=time.monotonic() - started)
    finally:
        _run_lock.release()


def reverify(net, agent, fn, proj, candidates: dict, ctx) -> dict:
    """P5.7 step 5: recompile and re-verify every upstream candidate through the coordinator. Only
    judge-verified improvements are recorded (the normal _record path); upstream scores are never
    trusted for acceptance. Returns per-name verdicts."""
    from .. import snapshots  # decompnet.snapshots

    snap = snapshots.capture(fn, proj, ctx)
    out = {}
    for name, source in (candidates or {}).items():
        if not isinstance(source, str) or not source.strip():
            out[name] = {"score": None, "accepted": False, "why": "empty candidate"}
            continue
        try:
            result = net._compile(proj, ctx["header"], source)
            if not result.ok:
                out[name] = {"score": None, "accepted": False, "why": "doesn't compile"}
                continue
            res = net._judge(agent, fn, proj, ctx, snap, source, result, scratch=False)
            out[name] = {"score": res.get("score"), "accepted": bool(res.get("matched")),
                         "why": "matched" if res.get("matched") else "judged, no improvement accepted yet"}
        except Exception as e:  # one odd candidate must not break the batch
            out[name] = {"score": None, "accepted": False, "why": f"{type(e).__name__}: {e}"}
    return out
