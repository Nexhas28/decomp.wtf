"""Mismatch-guided operator routing (plan P5.4).

The structured mismatch counts produced by `scoring.Comparison` (register, stack_offset, operand,
insertion, deletion, reorder) are routed to preferred operator families. Routing only *reweights*:
every operator keeps at least its base weight, so a routed family is never hard-excluded and the
search can always recover through another family.

Literal/data mismatches are never routed to generic permutation: the summary has no literal count,
so count-less-but-not-matching evidence is treated as "no structured evidence" and keeps the
conservative mixed portfolio, with the decision recording that data/constant representation is out
of the permuter's reach. Unknown or weakly classified evidence falls back to the same conservative
portfolio. The `RoutingDecision` is recorded with every run (P5.4: record the decision and outcome).
"""

from __future__ import annotations

from dataclasses import dataclass

from .policy import SearchPolicy

# P5.4 table, versioned: dominant mismatch evidence -> preferred operator families (PR-17 operator ids).
_ROUTE_V1: dict[str, tuple[str, ...]] = {
    # register allocation concentrated in otherwise aligned code:
    # declaration order, temporary lifetime, expression decomposition
    "register": ("reorder_decls", "temp_introduce", "temp_eliminate"),
    # stack offsets/layout: local ordering, scope changes, temporary introduction/elimination
    "stack_offset": ("reorder_decls", "temp_introduce", "temp_eliminate"),
    # extension, signed comparison or narrowing differences: types, casts, comparison interpretation
    "operand": ("retype_local", "cast_add", "cast_remove", "comparison_flip", "cond_bool_form"),
    # control-flow differences: conditions, early returns, loop shape
    "insertion": ("return_ternary", "branch_swap", "loop_bound_explore", "while_for", "inc_style"),
    "deletion": ("return_ternary", "branch_swap", "loop_bound_explore", "while_for", "inc_style"),
    # instruction scheduling: dependency-safe order changes and expression grouping
    "reorder": ("swap_statements", "temp_introduce", "temp_eliminate", "inc_style"),
}

ROUTING_TABLES = {"mismatch-v1": _ROUTE_V1}

_MISMATCH_KEYS = frozenset({"register", "stack_offset", "operand", "insertion", "deletion", "reorder"})
_DOMINANT_BOOST = 4
_SECONDARY_BOOST = 2

_FAMILY_NOTES = {
    "register": "declaration order, temporary lifetime, expression decomposition",
    "stack_offset": "local ordering, scope changes, temporary introduction/elimination",
    "operand": "types, casts, comparison interpretation",
    "insertion": "conditions, early returns, loop shape",
    "deletion": "conditions, early returns, loop shape",
    "reorder": "dependency-safe order changes and expression grouping",
}


@dataclass(frozen=True)
class RoutingDecision:
    """The recorded mismatch-to-operator decision (P5.4). Pure: same counts -> same decision."""

    routing: str
    policy_epoch: str
    counts: dict
    coverage: str
    families: tuple[str, ...]
    weights: dict  # operator id -> weight: the proposal policy for this run
    reason: str

    def to_dict(self) -> dict:
        return {"routing": self.routing, "policy_epoch": self.policy_epoch, "counts": dict(self.counts),
                "coverage": self.coverage, "families": list(self.families),
                "weights": dict(self.weights), "reason": self.reason}

    @staticmethod
    def from_dict(d: dict) -> "RoutingDecision":
        return RoutingDecision(routing=d["routing"], policy_epoch=d["policy_epoch"], counts=d["counts"],
                               coverage=d["coverage"], families=tuple(d["families"]),
                               weights=d["weights"], reason=d["reason"])


def route(counts: dict, coverage: str, policy: SearchPolicy) -> RoutingDecision:
    """Route structured mismatch counts to operator weights under `policy`'s routing table."""
    table = ROUTING_TABLES.get(policy.routing)
    if table is None:
        raise ValueError(f"unknown routing table {policy.routing!r} "
                         f"(registered: {', '.join(sorted(ROUTING_TABLES))})")
    unknown = set(counts) - _MISMATCH_KEYS
    if unknown:
        raise ValueError(f"unknown mismatch keys {sorted(unknown)}; expected {sorted(_MISMATCH_KEYS)}")
    weights = dict(policy.base_weights)
    present = {k: v for k, v in counts.items() if v > 0}
    families: tuple[str, ...] = ()
    if not present:
        reason = ("no structured mismatch evidence (possible literal/data or encoding-only "
                  "differences): no permutation family is boosted — conservative mixed portfolio; "
                  "data/constant representation is never reached through generic permutation")
    else:
        dominant = max(present, key=present.get)
        families = table[dominant]
        for op in families:
            weights[op] = weights.get(op, 1) * _DOMINANT_BOOST
        for key, n in present.items():
            if key == dominant:
                continue
            for op in table.get(key, ()):
                weights[op] = max(weights[op], policy.base_weights.get(op, 1) * _SECONDARY_BOOST)
        reason = (f"{dominant} mismatches dominate ({present[dominant]}); boosting "
                  f"{', '.join(families)} — {_FAMILY_NOTES[dominant]}")
    return RoutingDecision(routing=policy.routing, policy_epoch=policy.epoch, counts=dict(counts),
                           coverage=coverage, families=families, weights=weights, reason=reason)
