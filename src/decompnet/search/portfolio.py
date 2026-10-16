"""The bounded, diverse candidate portfolio (plan P5.5).

Replaces the single current/best trajectory with a beam (default 4, a tuning starting point):

* the best verified-under-current-snapshot source is always retained (never evicted, never replaced
  by a worse candidate);
* a small number of structurally different alternatives are kept, ranked by score plus diversity
  (diversity is the structural statement-kind signature from the PR-17 analyser — structure, not
  semantics — and it can never override final verification, which is the judge's score);
* downhill acceptance matches the legacy single-trajectory contract (`score <= parent` moves);
  a small, configurable plateau allowance admits occasional worse candidates;
* memory, source growth and step counts are bounded by the policy;
* candidates carry their mutation history, and the whole portfolio (with RNG state) checkpoints for
  resumable runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..permuter import Mutation, analyze
from .dedup import EvalOutcome
from .policy import SearchPolicy


def structure_key(source: str) -> str:
    """The analyser's structural signature: statement kinds + nesting + body bounds. Two sources that
    share a structure key differ only in token-level details; different keys mean different shapes."""
    an = analyze(source)
    kinds = "".join(s.kind[0] for s in an.stmts.values())
    depths = "".join(str(s.depth) for s in an.stmts.values())
    return f"{an.body}:{kinds}:{depths}"


@dataclass(frozen=True)
class Candidate:
    source: str
    score: int
    structure: str
    history: tuple = ()                 # mutation history: "operator:category" entries
    object_digest: str | None = None
    parent_digest: str | None = None

    def to_dict(self) -> dict:
        return {"source": self.source, "score": self.score, "structure": self.structure,
                "history": list(self.history), "object_digest": self.object_digest,
                "parent_digest": self.parent_digest}

    @staticmethod
    def from_dict(d: dict) -> "Candidate":
        return Candidate(source=d["source"], score=d["score"], structure=d["structure"],
                         history=tuple(d["history"]), object_digest=d.get("object_digest"),
                         parent_digest=d.get("parent_digest"))


class Portfolio:
    def __init__(self, policy: SearchPolicy, best_source: str, best_score: int):
        self.policy = policy
        self.best = Candidate(best_source, best_score, structure_key(best_source))
        self.members: list[Candidate] = [self.best]

    def pick_parent(self, rng) -> Candidate:
        """Rank-biased parent choice: the best gets the most proposals; all members are reachable."""
        n = len(self.members)
        order = sorted(range(n), key=lambda i: (self.members[i].score, i))  # best first
        weights = [0] * n
        for rank, i in enumerate(order):
            weights[i] = n - rank
        return rng.choices(self.members, weights=weights, k=1)[0]

    def build(self, parent: Candidate, mutation: Mutation, outcome: EvalOutcome) -> Candidate:
        history = parent.history + (f"{mutation.operator}:{mutation.category}",)
        return Candidate(source=mutation.text, score=outcome.score,
                         structure=structure_key(mutation.text),
                         history=history[-self.policy.max_history:],
                         object_digest=outcome.object_digest, parent_digest=mutation.parent_digest)

    def update(self, parent: Candidate, mutation: Mutation, outcome: EvalOutcome, rng) -> str:
        """Apply one evaluated mutation. Returns matched | improving | accepted | non_improving."""
        cand = self.build(parent, mutation, outcome)
        if outcome.score == 0:
            self.best = cand
            self._add(cand)
            return "matched"
        if outcome.score < self.best.score:
            self.best = cand
            self._add(cand)
            return "improving"
        if not (outcome.score <= parent.score or rng.random() < self.policy.plateau):
            return "non_improving"
        if parent is self.best:
            self._add(cand)  # equal-to-best diversity: keep the best pointer, bound the beam
            return "accepted"
        self.members[self.members.index(parent)] = cand
        return "accepted"

    def offer(self, cand: Candidate) -> bool:
        """Diversity-only admission (P5.6): keep a distinct same-object source when there is room."""
        if len(self.members) >= self.policy.beam:
            return False
        if any(m.structure == cand.structure or m.source == cand.source for m in self.members):
            return False
        self.members.append(cand)
        return True

    def _add(self, cand: Candidate) -> None:
        self.members.append(cand)
        self._evict()

    def _evict(self) -> None:
        """Diversity-aware eviction: never the best; prefer a member whose structure duplicates
        another member's, otherwise the worst score (ties: the later addition)."""
        while len(self.members) > self.policy.beam:
            idx = None
            for i, m in enumerate(self.members):
                if m is self.best:
                    continue
                if any(o is not m and o.structure == m.structure for o in self.members):
                    idx = i
                    break
            if idx is None:
                idx = max((i for i, m in enumerate(self.members) if m is not self.best),
                          key=lambda i: (self.members[i].score, i))
            self.members.pop(idx)
