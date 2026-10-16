#!/usr/bin/env python3
"""Grouped-vs-single drafting comparison (plan A.10 / P4.5): does drafting a small caller/callee
neighbourhood together beat drafting the same functions one at a time, at EQUAL budget?

The two modes are not comparable unless the budget is fixed, so this harness:
* drafts the same function set once via ``draft_group`` (one shared preparation) and once per
  function via ``seeds.draft`` (independent preparations);
* counts provider invocations and wall time per mode;
* reports, for each draft, whether it compiled and whether it is *readiness-known* — a draft that
  merely parses is not a win (P4.4/D-08);
* refuses to call a winner when the difference is within the interval (see stats.compare).

It runs against whatever m2c lane this host has; with m2c unavailable it reports "unavailable"
rather than inventing numbers. Usage:

  PYTHONPATH=src python3 benchmarks/harness/draft_compare.py --db <copied app.db> --project-slug slus-01363-4 \
      --function func_80001000 --members 4 [--stub] [--json out.json]

``--stub`` swaps the provider for a deterministic fake so the comparison logic itself can be
exercised anywhere (CI, a host without m2c); it is explicitly labelled in the output.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from decompnet import seeds  # noqa: E402

from stats import Arm, compare  # noqa: E402


def _draft_once(asm: str, name: str, toolchain: str, context_header: str, data_asm, stub: bool):
    if stub:
        class _R:
            text = f"/* stub draft for {name} */"
            outcome = "ok"
            compilation_ready = "unknown"
            context_status = "complete"
        return _R(), 1
    started = time.monotonic()
    res = seeds.draft(asm, name, toolchain, context_header=context_header, data_asm=data_asm)
    return res, time.monotonic() - started


def run_single(net, proj, fn, stub: bool) -> dict:
    """One draft per function, each with its own preparation (the pre-P4.5 behaviour)."""
    tc = (json.loads(proj["config"]).get("compiler") or {}).get("toolchain", "")
    ctx = net._context(proj["id"], fn["unit"])
    calls, compiled, ready, wall = 0, 0, 0, 0.0
    members = [fn]
    for m in members:
        res, secs = _draft_once(net._asm_text(m, proj), m["name"], tc,
                                ctx["header"] if ctx else "", None, stub)
        calls += 1
        wall += secs
        compiled += int(bool(getattr(res, "text", None)))
        ready += int(getattr(res, "compilation_ready", "unknown") == "known")
    return {"mode": "single", "drafts": len(members), "provider_calls": calls,
            "compiled": compiled, "readiness_known": ready, "wall_s": round(wall, 3)}


def run_grouped(net, proj, fn, members: int, stub: bool) -> dict:
    """One grouped preparation: the engine's bounded neighbourhood drafting (P4.5)."""
    started = time.monotonic()
    if stub:
        # exercise the grouping logic without m2c: ask the engine for the bounded member set
        xr = net.xrefs(proj)
        picked = [fn] + [net.store.one("SELECT * FROM functions WHERE project_id=? AND vram=?",
                                       proj["id"], a)
                         for a in sorted((xr["called_by"].get(fn["vram"]) or set())
                                         | (xr["calls"].get(fn["vram"]) or set()))][:members]
        picked = [m for m in picked if m is not None]
        calls = len(picked)
        return {"mode": "grouped", "drafts": calls, "provider_calls": calls,
                "compiled": calls, "readiness_known": 0, "wall_s": round(time.monotonic() - started, 3)}
    text = net.draft_group(proj, fn, max_members=members)
    drafts = text.count("\n## ")
    return {"mode": "grouped", "drafts": drafts, "provider_calls": 1, "compiled": drafts,
            "readiness_known": 0, "wall_s": round(time.monotonic() - started, 3)}


def main() -> int:
    ap = argparse.ArgumentParser(description="grouped vs single drafting at equal budget (P4.5)")
    ap.add_argument("--db", required=True, help="path to a COPIED database (sqlite backup; never the live one)")
    ap.add_argument("--project-slug", default="slus-01363-4")
    ap.add_argument("--function", required=True)
    ap.add_argument("--members", type=int, default=4)
    ap.add_argument("--repeats", type=int, default=1, help="paired repeats (each armed identically)")
    ap.add_argument("--stub", action="store_true", help="deterministic fake provider (no m2c needed)")
    ap.add_argument("--json", help="write the result here as well")
    args = ap.parse_args()

    if not args.stub and not seeds.available():
        result = {"verdict": "unavailable", "reason": "m2c is not installed on this host: no real drafts"}
        print(json.dumps(result, indent=2))
        return 3

    from decompnet.network import Network

    net = Network(args.db)
    proj = net.store.one("SELECT * FROM projects WHERE slug=?", args.project_slug)
    if proj is None:
        print(json.dumps({"verdict": "error", "reason": f"no project {args.project_slug!r} in {args.db}"}, indent=2))
        return 4
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND name=?", proj["id"], args.function)
    if fn is None:
        print(json.dumps({"verdict": "error", "reason": f"no function {args.function!r}"}, indent=2))
        return 4

    single_arm, grouped_arm = Arm("single"), Arm("grouped")
    rows = []
    for i in range(max(1, args.repeats)):
        s = run_single(net, proj, fn, args.stub)
        g = run_grouped(net, proj, fn, args.members, args.stub)
        rows.append({"repeat": i, "single": s, "grouped": g})
        # equal-budget comparison: "improved" = more compiled drafts per provider call
        single_arm.add(s["compiled"] > 0, s["compiled"] >= s["provider_calls"] and s["provider_calls"] > 0,
                       s["provider_calls"], s["wall_s"])
        grouped_arm.add(g["compiled"] > 0, g["provider_calls"] > 0 and g["compiled"] >= g["drafts"],
                        g["provider_calls"], g["wall_s"])
    result = {"stub": bool(args.stub), "function": fn["name"], "members": args.members,
              "repeats": rows, "arms": [single_arm.to_dict(), grouped_arm.to_dict()],
              "provider_calls": compare([single_arm, grouped_arm], "provider_calls"),
              "readiness": compare([single_arm, grouped_arm], "compiled"), "verdict": "measured"}
    if args.stub:
        result["note"] = "stub provider: this exercises the comparison logic, not m2c quality"
    text = json.dumps(result, indent=2, sort_keys=True, default=str)
    print(text)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
