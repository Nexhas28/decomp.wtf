"""Versioned search policy (plan P5.5/P5.8): one serializable spec for guided permutation.

A `SearchPolicy` is the complete, versioned description of how a search run behaves: the operator
base weights, beam/plateau/bounds, the routing-table version and the policy identity. It serializes
to plain JSON-able data, so a distributed worker can carry the exact policy the coordinator used
(P-08 groundwork: jobs carry "the exact target, compiler, context, policy and seed identities"; the
target/compiler/context identities travel in the attempt snapshot, the policy travels here).

A worker that does not know a policy's id/version rejects it via `parse_policy` instead of silently
running a different search (P-08: incompatible policy versions are rejected or explicitly resubmitted).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..permuter import DEFAULT_POLICY, OPERATORS


class SearchPolicyError(ValueError):
    """A policy spec this process cannot honour (unknown id/version or invalid fields)."""


@dataclass(frozen=True)
class SearchPolicy:
    """One immutable search-policy version. Bump `version` whenever routing or defaults change;
    `SEARCH_POLICY` is the process's current policy."""

    id: str
    version: int
    beam: int = 4                 # bounded diverse portfolio size (P5.5; a tuning starting point)
    plateau: float = 0.02         # small, configurable exploration allowance for plateaus
    max_source_chars: int = 200_000  # source-growth bound
    max_steps: int = 400          # compile/evaluation attempts per turn
    max_history: int = 64         # mutation-history entries kept per candidate
    routing: str = "mismatch-v1"  # routing-table version (P5.4)
    base_weights: dict = field(default_factory=lambda: dict(DEFAULT_POLICY))

    @property
    def epoch(self) -> str:
        """The search-policy epoch stamped on budget rows: a policy version change is a new epoch."""
        return f"{self.id}-v{self.version}"

    def to_dict(self) -> dict:
        return {"id": self.id, "version": self.version, "beam": self.beam, "plateau": self.plateau,
                "max_source_chars": self.max_source_chars, "max_steps": self.max_steps,
                "max_history": self.max_history, "routing": self.routing,
                "base_weights": dict(self.base_weights)}

    @staticmethod
    def from_dict(d: dict) -> "SearchPolicy":
        if not isinstance(d, dict) or "id" not in d or "version" not in d:
            raise SearchPolicyError("a search-policy spec needs string id and integer version")
        id_, version = d["id"], d["version"]
        if not isinstance(id_, str) or not id_:
            raise SearchPolicyError("policy id must be a non-empty string")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise SearchPolicyError("policy version must be a positive integer")
        beam = d.get("beam", 4)
        plateau = d.get("plateau", 0.02)
        max_source_chars = d.get("max_source_chars", 200_000)
        max_steps = d.get("max_steps", 400)
        max_history = d.get("max_history", 64)
        routing = d.get("routing", "mismatch-v1")
        if not isinstance(beam, int) or beam < 1:
            raise SearchPolicyError("beam must be a positive integer")
        if not isinstance(plateau, (int, float)) or not 0 <= plateau < 1:
            raise SearchPolicyError("plateau must be in [0, 1)")
        if not isinstance(max_source_chars, int) or max_source_chars < 0:
            raise SearchPolicyError("max_source_chars must be a non-negative integer")
        if not isinstance(max_steps, int) or max_steps < 0:
            raise SearchPolicyError("max_steps must be a non-negative integer")
        if not isinstance(max_history, int) or max_history < 1:
            raise SearchPolicyError("max_history must be a positive integer")
        if not isinstance(routing, str) or not routing:
            raise SearchPolicyError("routing must name a registered routing table")
        weights = d.get("base_weights", {})
        if not isinstance(weights, dict):
            raise SearchPolicyError("base_weights must be a dict of operator id -> positive weight")
        for op, w in weights.items():
            if op not in OPERATORS:
                raise SearchPolicyError(f"unknown operator {op!r} in base_weights")
            if not isinstance(w, int) or isinstance(w, bool) or w < 1:
                raise SearchPolicyError(f"weight for {op!r} must be a positive integer")
        return SearchPolicy(id=id_, version=version, beam=beam, plateau=float(plateau),
                            max_source_chars=max_source_chars, max_steps=max_steps,
                            max_history=max_history, routing=routing, base_weights=dict(weights))


SEARCH_POLICY = SearchPolicy(id="guided", version=1)

_REGISTRY: dict[str, dict[int, SearchPolicy]] = {SEARCH_POLICY.id: {SEARCH_POLICY.version: SEARCH_POLICY}}


def parse_policy(d: dict) -> SearchPolicy:
    """Resolve a serialized policy spec to this process's canonical policy object.

    Unknown id/version combinations raise `SearchPolicyError`: an older worker receiving a newer
    policy must reject the job rather than run a different search (P-08)."""
    if not isinstance(d, dict):
        raise SearchPolicyError("policy spec must be a dict")
    try:
        return _REGISTRY[d["id"]][d["version"]]
    except (KeyError, TypeError):
        registered = ", ".join(f"{k}-v{v}" for k, vs in sorted(_REGISTRY.items())
                               for v in sorted(vs))
        raise SearchPolicyError(f"unknown search policy {d.get('id')!r} version {d.get('version')!r}; "
                                f"registered: {registered or 'none'}")
