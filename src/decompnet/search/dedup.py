"""Evaluation dedup without diversity loss (plan P5.6).

Two digests are maintained per run:

* source digest -> evaluation result under the current snapshot (skip re-compiling a source the run
  has already seen; the compile cache handles cross-run compile reuse, PR-09/10)
* object digest -> evaluation result under the same target/judge snapshot (the candidate object's
  normalised fingerprint: two distinct sources compiling to identical judged code share one result)

Two distinct sources that compile identically keep separate portfolio entries (they support different
future rewrites); only the evaluation result is reused. Metrics distinguish duplicate-source
proposals, duplicate-object outputs, compile errors, evaluate failures and non-improving candidates
(the run tracks the last two in its stats).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EvalOutcome:
    """One evaluation result under a fixed snapshot. score None means "not a scored candidate"."""

    score: int | None
    bytes_match: bool = False
    counts: dict | None = None       # structured mismatch counts (P5.4 routing feedback)
    object_digest: str | None = None  # judged-code identity (normalised fingerprint), when available
    kind: str = "scored"             # scored | fork | compile_error | evaluate_failure

    def to_dict(self) -> dict:
        return {"score": self.score, "bytes_match": self.bytes_match, "counts": self.counts,
                "object_digest": self.object_digest, "kind": self.kind}

    @staticmethod
    def from_dict(d: dict) -> "EvalOutcome":
        return EvalOutcome(score=d["score"], bytes_match=d.get("bytes_match", False),
                           counts=d.get("counts"), object_digest=d.get("object_digest"),
                           kind=d.get("kind", "scored"))


class EvalCache:
    """Per-run evaluation reuse, valid under one snapshot key (target/context/compiler/scorer)."""

    def __init__(self, snapshot_key: str):
        self.snapshot_key = snapshot_key
        self.sources: dict[str, EvalOutcome] = {}
        self.objects: dict[str, EvalOutcome] = {}

    def source(self, digest: str) -> EvalOutcome | None:
        return self.sources.get(digest)

    def object(self, digest: str) -> EvalOutcome | None:
        return self.objects.get(digest)

    def store(self, digest: str, outcome: EvalOutcome) -> None:
        """Record an outcome under its source digest, and under its object digest when present."""
        self.sources[digest] = outcome
        if outcome.object_digest:
            self.objects.setdefault(outcome.object_digest, outcome)

    def to_dict(self) -> dict:
        return {"snapshot_key": self.snapshot_key,
                "sources": {k: v.to_dict() for k, v in self.sources.items()},
                "objects": {k: v.to_dict() for k, v in self.objects.items()}}

    @staticmethod
    def from_dict(d: dict) -> "EvalCache":
        cache = EvalCache(d["snapshot_key"])
        cache.sources = {k: EvalOutcome.from_dict(v) for k, v in d["sources"].items()}
        cache.objects = {k: EvalOutcome.from_dict(v) for k, v in d["objects"].items()}
        return cache
