#!/usr/bin/env python3
"""Holdout recall harness (plan A.7/P12.2): does retrieval actually find the answers it must not have
been tuned on — and does reuse pay off under verification?

It measures two things separately, because they are different claims:

* **recall@k** — of the holdout answers registered in a retrieval registry, how many appear in the
  top-k candidates of the route plan (per route and overall);
* **verified-reuse rate** — of the candidates that were actually surfaced, how many led to a
  coordinator-verified match (the only credit that counts).

No tuning happens here: the registry's holdout markers are the evaluation set, and the script refuses
to run when the corpus contains a holdout answer (that would be leakage, not recall). Run:

  PYTHONPATH=src python3 benchmarks/harness/holdout_recall.py --registry corpus.json --k 5 [--json out.json]

The registry is the JSON form of decompnet.retrieval.Registry (to_json/from_json). Candidates come
from the same route plan the engine uses (retrieval.route_plan); with every flag off the plan is
empty and the result is an explicit "disabled" line rather than a misleading zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from decompnet import retrieval  # noqa: E402

from stats import wilson  # noqa: E402  (same directory)


def load_registry(path: str | Path) -> retrieval.Registry:
    return retrieval.Registry.from_json(Path(path).read_text(encoding="utf-8"))


def holdout_answers(registry: retrieval.Registry) -> dict:
    """{answer_id: {source_digest, target_digest, source_location}} for holdout-marked entries."""
    out = {}
    for entry in registry.entries().values():
        if not entry.holdout:
            continue
        out[entry.entry_id] = {
            "source_digest": entry.source_digest,
            "target_digest": entry.target_identity,
            "source_location": entry.source_location,
        }
    return out


def leakage(registry: retrieval.Registry, answers: dict) -> list:
    return registry.leakage_check(answers) if answers else []


def recall_at_k(candidates: list, answers: dict, k: int) -> dict:
    """candidates: ranked list of dicts with at least {'target_digest'|'evidence'} identity. An answer
    counts as recalled when one of the top-k candidates carries the same target identity."""
    wanted = {a.get("target_digest") for a in answers.values() if a.get("target_digest")}
    if not wanted:
        return {"answers": 0, "k": k, "recalled": 0, "recall": None, "recall_ci95": [0.0, 1.0]}
    top = candidates[:k]
    got = {c.get("target_digest") or c.get("target_identity") for c in top}
    hit = len(wanted & got)
    lo, hi = wilson(hit, len(wanted))
    return {"answers": len(wanted), "k": k, "recalled": hit,
            "recall": hit / len(wanted), "recall_ci95": [round(lo, 4), round(hi, 4)]}


def verified_reuse_rate(outcomes: list) -> dict:
    """outcomes: [{'surfaced': bool, 'verified': bool}]. Verified reuse counts only surfaced
    candidates that the coordinator accepted — a retrieval score is never a match."""
    surfaced = [o for o in outcomes if o.get("surfaced")]
    if not surfaced:
        return {"surfaced": 0, "verified": 0, "verified_reuse_rate": None, "verified_reuse_ci95": [0.0, 1.0]}
    verified = sum(1 for o in surfaced if o.get("verified"))
    lo, hi = wilson(verified, len(surfaced))
    return {"surfaced": len(surfaced), "verified": verified,
            "verified_reuse_rate": verified / len(surfaced),
            "verified_reuse_ci95": [round(lo, 4), round(hi, 4)]}


def main() -> int:
    ap = argparse.ArgumentParser(description="holdout recall for the retrieval routes (no tuning)")
    ap.add_argument("--registry", required=True, help="Registry JSON (retrieval.Registry.to_json)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--candidates", help="optional JSONL of ranked candidates {target_digest|target_identity}")
    ap.add_argument("--outcomes", help="optional JSONL {surfaced, verified} from a real run")
    ap.add_argument("--json", help="write the result here as well")
    args = ap.parse_args()

    registry = load_registry(args.registry)
    answers = holdout_answers(registry)
    leaks = leakage(registry, answers)
    result = {"registry_entries": len(registry), "holdout_answers": len(answers),
              "leakage": [{"entry_id": l.entry_id, "answer_id": l.answer_id, "kind": l.kind,
                           "note": l.note} for l in leaks],
              "config_enabled": bool(retrieval.RetrievalConfig(function_id_retrieval=True,
                                                               bsim_retrieval=True))}
    if leaks:
        result["verdict"] = "refused: the corpus contains holdout answers (leakage, not recall)"
        print(json.dumps(result, indent=2))
        return 2
    if args.candidates:
        cands = [json.loads(line) for line in Path(args.candidates).read_text().splitlines() if line.strip()]
        result["recall"] = recall_at_k(cands, answers, args.k)
    if args.outcomes:
        outs = [json.loads(line) for line in Path(args.outcomes).read_text().splitlines() if line.strip()]
        result["verified_reuse"] = verified_reuse_rate(outs)
    result.setdefault("verdict", "measured")
    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
