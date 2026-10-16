"""The guided search run: routing + portfolio + dedup orchestration (plan P5.4-P5.6).

One `SearchRun` is one deterministic search trajectory over one function:

* seeded from (function id, turn, policy epoch) — the same seed, source, policy and snapshot replay
  the same mutation/evaluation sequence (P-04 groundwork);
* every step proposes through the routed operator weights, reuses evaluations by source and object
  digest, and applies the portfolio's bounded diversity rules (P5.5/P5.6);
* every step's outcome is recorded (operator, category, kind, score delta), alongside the run's
  routing decision, so the mismatch-to-operator decision and its result are reproducible;
* `checkpoint_run`/`resume_run` persist candidates, evaluation caches and RNG state: a resumed run
  continues exactly where it stopped (independently reproducible restart via the recorded seed).
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import asdict, dataclass, field

from ..permuter import FALLBACK, Mutation, analyze, digest, propose
from .dedup import EvalCache, EvalOutcome
from .policy import SearchPolicy
from .portfolio import Candidate, Portfolio
from .routing import RoutingDecision, route


class SearchResumeError(ValueError):
    """A checkpoint cannot be resumed here (stale snapshot or policy); start a fresh run instead."""


@dataclass(frozen=True)
class StepResult:
    """One recorded step outcome. Spans are primitive tuples so records stay JSON-safe."""

    step: int
    kind: str  # matched|improving|accepted|non_improving|duplicate_source|duplicate_object|
    #          # compile_error|evaluate_failure|size_rejected|propose_failure
    operator: str | None = None
    category: str | None = None
    parent_digest: str | None = None
    child_digest: str | None = None
    score: int | None = None
    score_delta: int | None = None
    spans: tuple = ()


@dataclass
class SearchRun:
    policy: SearchPolicy
    seed: str
    snapshot_key: str
    routing: RoutingDecision
    portfolio: Portfolio
    evals: EvalCache
    rng: random.Random
    slot_seeds: tuple[str, ...]  # independently reproducible restart seeds, one per beam slot
    steps: int = 0
    outcomes: list[StepResult] = field(default_factory=list)
    stats: Counter = field(default_factory=Counter)

    @property
    def matched(self) -> bool:
        return self.portfolio.best.score == 0

    def summary(self) -> dict:
        """Everything PR-19 / reporting needs: policy, seed, routing, beam, metrics, history."""
        return {
            "policy": self.policy.to_dict(),
            "seed": self.seed,
            "snapshot_key": self.snapshot_key,
            "steps": self.steps,
            "matched": self.matched,
            "best_score": self.portfolio.best.score,
            "best_source": self.portfolio.best.source,
            "beam": [m.to_dict() for m in self.portfolio.members],
            "routing": self.routing.to_dict(),
            "stats": dict(self.stats),
            "outcomes": [asdict(o) for o in self.outcomes],
        }


def new_run(policy: SearchPolicy, seed: str, snapshot_key: str, best_source: str, best_score: int,
            *, counts: dict | None = None, coverage: str = "unknown") -> SearchRun:
    """A fresh run: deterministic RNG from the seed, mismatch-routed weights, seeded portfolio."""
    rng = random.Random(seed)
    return SearchRun(
        policy=policy, seed=seed, snapshot_key=snapshot_key,
        routing=route(counts or {}, coverage, policy),
        portfolio=Portfolio(policy, best_source, best_score),
        evals=EvalCache(snapshot_key), rng=rng,
        slot_seeds=tuple(f"{seed}:slot{i}" for i in range(policy.beam)))


def _default_propose(run: SearchRun) -> tuple[Candidate, Mutation | None]:
    parent = run.portfolio.pick_parent(run.rng)
    analysis = analyze(parent.source)
    run.stats["unsupported_constructs"] += len(analysis.unsupported)
    mutation = propose(parent.source, run.rng, run.routing.weights, analysis=analysis)
    return parent, mutation


def _apply(run: SearchRun, parent: Candidate, mutation: Mutation, outcome: EvalOutcome,
           dup_kind: str | None) -> StepResult:
    kind = run.portfolio.update(parent, mutation, outcome, run.rng)
    run.stats[kind] += 1  # matched | improving | accepted | non_improving
    if dup_kind == "duplicate_object" and kind == "non_improving":
        # distinct source, identical object: reuse the result but keep the source for diversity (P5.6)
        run.portfolio.offer(run.portfolio.build(parent, mutation, outcome))
    score_delta = None
    if outcome.score is not None and parent is not None:
        score_delta = outcome.score - parent.score
    spans = tuple((s.start_line, s.start_col, s.end_line, s.end_col) for s in mutation.spans)
    res = StepResult(run.steps, dup_kind or kind, mutation.operator, mutation.category,
                     mutation.parent_digest, mutation.child_digest, outcome.score, score_delta, spans)
    run.outcomes.append(res)
    return res


def step(run: SearchRun, eval_fn, propose=None) -> StepResult | None:
    """One proposal->dedup->evaluation->portfolio step. Returns None when the run is finished
    (step budget spent or already matched): callers use it as the loop guard."""
    if run.steps >= run.policy.max_steps or run.portfolio.best.score == 0 or not run.portfolio.members:
        return None
    run.steps += 1
    parent, mutation = (propose or _default_propose)(run)
    if mutation is None:
        run.stats["propose_failure"] += 1
        res = StepResult(run.steps, "propose_failure")
        run.outcomes.append(res)
        return res
    if mutation.category == FALLBACK:
        run.stats["fallback_mutation"] += 1
    if len(mutation.text) > run.policy.max_source_chars:
        run.stats["size_rejected"] += 1
        res = StepResult(run.steps, "size_rejected", mutation.operator, mutation.category,
                         mutation.parent_digest, mutation.child_digest)
        run.outcomes.append(res)
        return res
    d = digest(mutation.text)
    cached = run.evals.source(d)
    if cached is not None:
        run.stats["duplicate_source"] += 1
        if cached.score is None:  # the earlier evaluation was a failure: reuse it, never re-apply
            res = StepResult(run.steps, "duplicate_source", mutation.operator, mutation.category,
                             mutation.parent_digest, mutation.child_digest)
            run.outcomes.append(res)
            return res
        return _apply(run, parent, mutation, cached, "duplicate_source")
    outcome = eval_fn(mutation.text)
    if outcome is None:
        outcome = EvalOutcome(score=None, kind="evaluate_failure")
    run.stats[outcome.kind if outcome.kind in ("scored", "fork", "compile_error", "evaluate_failure")
              else "scored"] += 1
    if outcome.kind in ("compile_error", "evaluate_failure"):
        run.evals.store(d, outcome)
        res = StepResult(run.steps, outcome.kind, mutation.operator, mutation.category,
                         mutation.parent_digest, mutation.child_digest)
        run.outcomes.append(res)
        return res
    if outcome.object_digest:
        prior = run.evals.object(outcome.object_digest)
        if prior is not None:
            run.stats["duplicate_object"] += 1
            run.evals.store(d, outcome)
            return _apply(run, parent, mutation, prior, "duplicate_object")
    run.evals.store(d, outcome)
    return _apply(run, parent, mutation, outcome, None)


# ------------------------------------------------------------- checkpointing

def _rng_to_json(state) -> dict:
    version, ints, gauss = state
    return {"version": version, "state": list(ints), "gauss": gauss}


def _rng_from_json(d: dict):
    return (d["version"], tuple(d["state"]), d["gauss"])


def checkpoint_run(run: SearchRun) -> dict:
    """A JSON-safe checkpoint: candidates, evaluation caches, stats and RNG state (P5.5 step 7)."""
    return {
        "policy": run.policy.to_dict(),
        "seed": run.seed,
        "snapshot_key": run.snapshot_key,
        "steps": run.steps,
        "rng": _rng_to_json(run.rng.getstate()),
        "slot_seeds": list(run.slot_seeds),
        "routing": run.routing.to_dict(),
        "members": [m.to_dict() for m in run.portfolio.members],
        "best_source": run.portfolio.best.source,
        "evals": run.evals.to_dict(),
        "stats": dict(run.stats),
        "outcomes": [asdict(o) for o in run.outcomes],
    }


def resume_run(policy: SearchPolicy, snapshot_key: str, cp: dict) -> SearchRun:
    """Rebuild a run from its checkpoint; rejects stale snapshots and policy changes (P-08)."""
    if cp.get("snapshot_key") != snapshot_key:
        raise SearchResumeError(f"checkpoint snapshot {cp.get('snapshot_key')!r} != current "
                                f"{snapshot_key!r}")
    saved = cp.get("policy", {})
    if saved.get("id") != policy.id or saved.get("version") != policy.version:
        raise SearchResumeError(f"checkpoint policy {saved.get('id')!r}-v{saved.get('version')} "
                                f"!= current {policy.id}-v{policy.version}")
    members = [Candidate.from_dict(d) for d in cp["members"]]
    best = next((m for m in members if m.source == cp["best_source"]), members[0])
    rng = random.Random()
    rng.setstate(_rng_from_json(cp["rng"]))
    run = SearchRun(
        policy=policy, seed=cp["seed"], snapshot_key=cp["snapshot_key"],
        routing=RoutingDecision.from_dict(cp["routing"]),
        portfolio=Portfolio(policy, best.source, best.score),
        evals=EvalCache.from_dict(cp["evals"]), rng=rng,
        slot_seeds=tuple(cp["slot_seeds"]), steps=cp["steps"],
        outcomes=[StepResult(**d) for d in cp["outcomes"]], stats=Counter(cp["stats"]))
    run.portfolio.members = members
    run.portfolio.best = best
    return run
