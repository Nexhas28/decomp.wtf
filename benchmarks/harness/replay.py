#!/usr/bin/env python3
"""Fixed-input replay harness (plan P0.4): re-run recorded attempts through the same coordinator
infrastructure with a deterministic compiler stub, capturing per-stage timings and compile counts.

Separate from live-agent experiments: no model randomness, no real toolchains. Stage timings use
monotonic clocks; UTC timestamps for cross-process correlation. Everything lands under
benchmark-results/<run-id>/ (see plan Section 21.6 layout).

Usage:
  PYTHONPATH=src .venv/bin/python benchmarks/harness/replay.py \
      --db /tmp/replay.db --project fixture --attempts fixtures/replay-attempts.jsonl \
      --stub compile-error|score-N|match

The attempts file is JSONL: {"function": "...", "source": "..."} (optionally {"expect": "match"}).
Run against a synthetic project (tests/test_permute.py::make_project builds one) or any real project
you are willing to replay into (replay does not mark real matches when --dry-record is off: with the
stub compiler no real object is produced, so nothing can be falsely accepted; a 'match' stub only
exercises the record path and is meant for synthetic databases).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from decompnet.network import Network  # noqa: E402


_SECRET_HINTS = ("TOKEN", "SECRET", "PASSWORD", "KEY")


def _decompnet_env() -> dict:
    """The DECOMPNET_* environment this run saw; secret-looking values are redacted."""
    return {k: ("<redacted>" if any(h in k for h in _SECRET_HINTS) else v)
            for k, v in sorted(os.environ.items()) if k.startswith("DECOMPNET_")}


def _feature_flags() -> dict:
    """Optional features as the coordinator reads them from the environment (all off unless set)."""
    return {"guided_search": os.environ.get("DECOMPNET_GUIDED_SEARCH", "0") == "1",
            "rea_provider": os.environ.get("DECOMPNET_REA_PROVIDER") == "1",
            "structural_rerank": os.environ.get("DECOMPNET_STRUCTURAL_RERANK") == "1",
            "compile_cache": os.environ.get("DECOMPNET_COMPILE_CACHE") != "0"}


def _provider_status() -> list[dict]:
    """One row per optional provider: available true/false and why. Probes only; never starts one."""
    rows = []

    def add(provider: str, available: bool, reason: str) -> None:
        rows.append({"provider": provider, "available": bool(available), "reason": reason})

    try:
        from decompnet.forks import ghidra
        ok = ghidra.available()
        add("ghidra", ok, "analyzeHeadless and a Java runtime found" if ok
            else "analyzeHeadless and/or a Java runtime not found on this host")
    except Exception as e:
        add("ghidra", False, f"probe failed: {e}")
    try:
        from decompnet.analysis import rea
        ok = rea.available()
        why = "pinned REA install and node found" if ok else "pinned REA install or node not found"
        if os.environ.get("DECOMPNET_REA_PROVIDER") != "1":
            why += "; not enabled (DECOMPNET_REA_PROVIDER is not 1)"
        add("rea", ok, why)
    except Exception as e:
        add("rea", False, f"probe failed: {e}")
    try:
        from decompnet.retrieval.bsim import toolchain_available
        ok, why = toolchain_available()
    except Exception as e:
        ok, why = False, f"probe failed: {e}"
    add("fid", ok, why)
    add("bsim", ok, why)
    return rows


def _observed_receipts(net: Network, since_attempt_id: int) -> list[dict]:
    """Verification receipts written for attempts recorded after `since_attempt_id` (this run's)."""
    rows = net.store.all(
        "SELECT r.*, f.name AS function FROM verification_receipts r "
        "JOIN functions f ON f.id=r.function_id WHERE r.attempt_id > ? ORDER BY r.function_id",
        since_attempt_id)
    return [dict(r) for r in rows]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


@dataclass
class Stage:
    name: str
    started: float
    done: float = 0.0
    result: str = ""


class Timings:
    """Monotonic-clock stage recorder."""

    def __init__(self):
        self.stages: list[Stage] = []

    def span(self, name: str):
        return _Span(self, name)


class _Span:
    def __init__(self, timings: Timings, name: str):
        self.timings, self.name, self.stage = timings, name, Stage(name, time.monotonic())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stage.done = time.monotonic()
        self.timings.stages.append(self.stage)
        return False

    def note(self, result: str):
        self.stage.result = result


def stub_compiler(mode: str):
    """A deterministic compiler substitute. The fake object is not an ELF, so the harness also stubs
    evaluation: 'score-N'/'compile-error' produce a 'compiled but no such function' outcome (exercises
    compile+evaluate+record); 'match' produces a candidate whose fingerprint equals the target's —
    only meaningful for hash-only fixtures (target IS NULL, fingerprint set)."""

    class FakeResult:
        def __init__(self, ok, log="", obj=None):
            self.ok, self.log, self.obj = ok, log, obj

    def fake(proj, header, source, fn=None):  # fn: the merged _compile passes the function
        if mode == "compile-error":
            return FakeResult(False, "replay: deterministic compile error")
        return FakeResult(True, "", b"\x00replay")

    return fake


def stub_evaluator(mode: str):
    from decompnet.elfobj import FunctionObj

    class FakeCand(FunctionObj):
        def __init__(self, name):
            super().__init__(name, "mips", False, b"")
            self._fp = "replay-fingerprint"

        def fingerprint(self):
            return self._fp

    def fake(fn, proj, obj):
        if mode == "match":
            cand = FakeCand(fn["name"])
            cand._fp = fn["fingerprint"]
            return cand, True, []
        return None, None, []

    return fake


def replay(net: Network, agent, slug: str, attempts: list[dict], stub: str, timings: Timings):
    rows = []
    for i, entry in enumerate(attempts):
        fn_name, source, expect = entry["function"], entry["source"], entry.get("expect", "")
        net._compile = stub_compiler(stub)        # stage 1: the (stubbed) compiler subprocess
        net._evaluate = stub_evaluator(stub)      # stage 2: the (stubbed) object extraction/judge
        events = {}
        with timings.span(f"attempt-{i}") as total:
            with timings.span(f"attempt-{i}.claim") as s:
                try:
                    net.claim_row(agent, slug, fn_name)  # lease before work (claim is a no-op if held)
                    s.note("ok")
                except Exception as e:
                    s.note(f"error: {e}")
            try:
                out = net.attempt(agent, fn_name, source, slug)
                events["text"] = out["text"][:200]
                events["score"] = out.get("score")
                events["matched"] = out.get("matched")
            except Exception as e:
                events["error"] = str(e)
            if not events.get("matched"):
                try:
                    net.release(agent, fn_name, "replay step")  # next attempt gets the lease
                except Exception:
                    pass
            with timings.span(f"attempt-{i}.record-read") as s:
                row = net.store.one("SELECT a.score, a.compiled, a.via FROM attempts a "
                                    "WHERE a.function_id=(SELECT id FROM functions WHERE name=? AND project_id="
                                    "(SELECT id FROM projects WHERE slug=?)) ORDER BY a.id DESC LIMIT 1",
                                    fn_name, slug)
                s.note("ok" if row else "none")
        total.note("done" if "error" not in events else "error")
        rows.append({"attempt": i, "function": fn_name, "expect": expect, **events,
                     "score_recorded": row["score"] if row else None,
                     "via_recorded": row["via"] if row else None})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="fixed-input replay harness (plan P0.4)")
    ap.add_argument("--db", required=True, help="database to replay into (a copy or a synthetic one)")
    ap.add_argument("--project", required=True, help="project slug")
    ap.add_argument("--attempts", required=True, help="JSONL file of {function, source, expect}")
    ap.add_argument("--agent", default="replay", help="agent name to run as (created if absent)")
    ap.add_argument("--stub", default="score-5",
                    choices=["compile-error", "score-0", "score-5", "match"],
                    help="deterministic compiler substitute")
    ap.add_argument("--out", default="benchmark-results", help="results root (default benchmark-results)")
    args = ap.parse_args()

    run_id = f"replay-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:6]}"
    out = Path(args.out) / run_id
    out.mkdir(parents=True, exist_ok=True)

    attempts = [json.loads(line) for line in Path(args.attempts).read_text().splitlines() if line.strip()]
    started_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    net = Network(args.db)
    try:
        agent = net.authenticate(net.register_agent(args.agent))
    except Exception:
        # the name exists (e.g. a previous run): this replay env has no tokens, so mint via the store
        row = net.store.one("SELECT id, name, owner, trusted FROM agents WHERE name=?", args.agent)
        agent = type("A", (), {"id": row["id"], "name": row["name"], "owner": row["owner"],
                               "trusted": bool(row["trusted"])})()

    first_attempt = net.store.one("SELECT COALESCE(MAX(id), 0) AS n FROM attempts")["n"]
    timings = Timings()
    with timings.span("replay") as whole:
        rows = replay(net, agent, args.project, attempts, args.stub, timings)
    whole.note("done")

    manifest = {
        "schema_version": 1, "run_id": run_id, "started_utc": started_utc,
        "kind": "fixed-input-replay", "stub": args.stub,
        "db": str(Path(args.db).resolve()), "project": args.project,
        "attempts_in": len(attempts),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    effective = {
        "schema_version": 1, "run_id": run_id, "harness": Path(__file__).name,
        "args": vars(args), "stub": args.stub,
        "feature_flags": _feature_flags(), "env": _decompnet_env(),
    }
    (out / "effective-config.json").write_text(json.dumps(effective, indent=2, sort_keys=True) + "\n")
    corpus = {
        "schema_version": 1, "run_id": run_id, "kind": "fixed-input-replay", "project": args.project,
        "attempts_file": Path(args.attempts).name,
        "attempts_file_sha256": hashlib.sha256(Path(args.attempts).read_bytes()).hexdigest(),
        "fixtures": [{"index": i, "name": e["function"], "expect": e.get("expect", ""),
                      "source_sha256": hashlib.sha256(e["source"].encode("utf-8")).hexdigest()}
                     for i, e in enumerate(attempts)],
    }
    (out / "corpus-manifest.json").write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n")
    _write_jsonl(out / "verification-receipts.jsonl", _observed_receipts(net, first_attempt))
    _write_jsonl(out / "provider-status.jsonl", _provider_status())
    (out / "attempts.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (out / "stage-timings.jsonl").write_text(
        "\n".join(json.dumps({"name": s.name, "ms": round(1000 * (s.done - s.started), 3), "result": s.result})
                  for s in timings.stages) + "\n")
    summary = {
        "total_attempts": len(rows), "errors": sum(1 for r in rows if "error" in r),
        "matches": sum(1 for r in rows if r.get("matched")),
        "compiles": len(rows),  # one (stubbed) compile per attempt by construction
        "stage_ms": {s.name: round(1000 * (s.done - s.started), 3) for s in timings.stages},
        "expected_met": sum(1 for r in rows if r.get("expect") and (r.get("matched") == (r["expect"] == "match"))),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"run {run_id}: {summary['total_attempts']} attempts, {summary['errors']} errors, "
          f"{summary['matches']} matches, {summary['expected_met']} expectations met")
    print(f"artifacts: {out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
