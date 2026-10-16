"""Mismatch-guided operator routing (plan P5.4).

The structured mismatch counts produced by `scoring.Comparison` (register, stack_offset, operand,
insertion, deletion, reorder) are routed to preferred operator families. Routing only *reweights*:
every operator keeps a positive weight, so a routed family is never hard-excluded and the search can
always recover through another family. Boosts never go below the base weight; the one down-weighting
is for dominant call/global reference differences (`call_reference`, an informational subset of
`operand`), where prototype/context investigation comes before broad random mutation.

Literal/data mismatches are never routed to generic permutation: the summary has no literal count,
so count-less-but-not-matching evidence is treated as "no structured evidence" and boosts the
exploratory constant/representation operators (retype_local, cast_add, cast_remove) while
down-weighting structural reordering, with the decision recording that literal/data is suspected.
Unknown or weakly classified evidence falls back to the same conservative portfolio. The `RoutingDecision` is recorded with every run (P5.4: record the decision and outcome).
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
    # call/global reference differences: prototype/context investigation before broad random mutation.
    # These type-facing operators stay at base weight; every other operator is down-weighted.
    "call_reference": ("retype_local", "cast_add", "cast_remove"),
}

ROUTING_TABLES = {"mismatch-v1": _ROUTE_V1}

_MISMATCH_KEYS = frozenset({"register", "stack_offset", "operand", "insertion", "deletion", "reorder",
                            "call_reference"})
_DOMINANT_BOOST = 4
_SECONDARY_BOOST = 2
_CALL_REFERENCE_DOWNWEIGHT = 0.5
_LITERAL_DATA_FAMILIES = ("retype_local", "cast_add", "cast_remove")
_STRUCTURAL_REORDER = frozenset({"reorder_decls", "swap_statements", "temp_introduce", "temp_eliminate"})
_REORDER_DOWNWEIGHT = 0.5

_FAMILY_NOTES = {
    "register": "declaration order, temporary lifetime, expression decomposition",
    "stack_offset": "local ordering, scope changes, temporary introduction/elimination",
    "operand": "types, casts, comparison interpretation",
    "insertion": "conditions, early returns, loop shape",
    "deletion": "conditions, early returns, loop shape",
    "reorder": "dependency-safe order changes and expression grouping",
    "call_reference": "prototype/context investigation first",
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
    call_reference = present.pop("call_reference", 0)
    if call_reference:  # an informational subset of "operand": only the remaining operand mismatches count
        remaining = present.pop("operand", 0) - call_reference
        if remaining > 0:
            present["operand"] = remaining
        present["call_reference"] = call_reference
    families: tuple[str, ...] = ()
    if not present:
        families = _LITERAL_DATA_FAMILIES
        for op in families:
            weights[op] = weights.get(op, 1) * _DOMINANT_BOOST
        for op in weights:
            if op in _STRUCTURAL_REORDER:
                weights[op] = weights[op] * _REORDER_DOWNWEIGHT
        reason = "literal/data suspected: try constant representation and data declarations"
    else:
        dominant = max(present, key=present.get)
        families = table[dominant]
        if dominant == "call_reference":
            for op in weights:
                if op not in families:
                    weights[op] = weights[op] * _CALL_REFERENCE_DOWNWEIGHT
            reason = (f"call_reference mismatches dominate ({present[dominant]}); "
                      f"{_FAMILY_NOTES[dominant]}: down-weighting broad random mutation, keeping "
                      f"{', '.join(families)} at base weight")
        else:
            for op in families:
                weights[op] = weights.get(op, 1) * _DOMINANT_BOOST
            reason = (f"{dominant} mismatches dominate ({present[dominant]}); boosting "
                      f"{', '.join(families)} — {_FAMILY_NOTES[dominant]}")
        for key, n in present.items():
            if key == dominant or key == "call_reference":
                continue
            for op in table.get(key, ()):
                weights[op] = max(weights[op], policy.base_weights.get(op, 1) * _SECONDARY_BOOST)
    return RoutingDecision(routing=policy.routing, policy_epoch=policy.epoch, counts=dict(counts),
                           coverage=coverage, families=families, weights=weights, reason=reason)
