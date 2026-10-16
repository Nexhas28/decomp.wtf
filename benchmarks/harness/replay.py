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
import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from decompnet.network import Network  # noqa: E402


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
