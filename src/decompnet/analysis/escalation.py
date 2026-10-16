"""Escalation policy, focused investigations and the versioned candidate portfolio (P8.4-P8.6).

The policy consumes decomp.wtf's own structured mismatch summary (``scoring.mismatch_summary`` counts:
register, stack_offset, operand, insertion, deletion, reorder) plus function-level state, and decides the
next cheap action. Ghidra/REA analysis is only one of the actions, and only for structural/control-flow
uncertainty — never automatically after every fourth attempt or every score below a fixed percentage.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from .. import scoring
from .contracts import (BrokerError, Budget, CandidateChange, CandidateRecord, EvidenceBundle, Fact,
                        Hypothesis, Investigation, StaleEvidenceError)

#: The policy reads decomp.wtf's structured mismatch summary (P3.4). Guard against schema drift: the
#: count names the policy classifies on must stay exactly the scoring schema's.
assert set(scoring.COUNT_PENALTIES) == {"register", "stack_offset", "operand", "insertion", "deletion",
                                        "reorder"}, "scoring mismatch schema drifted"
_REGISTER_KEYS = ("register", "stack_offset")  # register/stack differences: a permutation problem
_STRUCTURAL_KEYS = ("insertion", "deletion", "reorder")  # instruction-stream differences: evidence first

#: Rule kinds the policy can route to (P8.4 default table). Rules can be disabled automatically when
#: they keep costing without producing verified progress (P8.7).
RULE_KINDS = ("twin_source", "m2c_draft", "context_repair", "permutation", "investigate_function",
              "investigate_neighbourhood", "profile_calibration", "release_cooldown", "data_literal_check")


@dataclass
class EscalationInputs:
    """Everything the policy is allowed to see (P8.4): mismatch category, source quality, recent
    progress, compiler-profile confidence, prior investigation and available budget."""

    function_key: str = ""
    snapshot: str = "current"
    mismatch: dict | None = None  # scoring.mismatch_summary(...) result
    score_percent: float | None = None
    attempts: int = 0  # valid, non-improving attempts so far
    twin_available: bool = False
    promising_source: bool = False
    m2c_tried: bool = False
    compile_errors: tuple = ()  # e.g. ("missing declaration foo", ...)
    compiler_profile_confidence: float | None = None
    prior_investigations: int = 0
    recent_progress: bool = False
    new_evidence_expected: bool = False
    data_literal_mismatch: bool = False
    conflicting_cross_function_evidence: bool = False
    control_flow_uncertain: bool = False
    jump_table_uncertain: bool = False
    budget: Budget = field(default_factory=Budget)

    @property
    def missing_declarations(self) -> tuple:
        return tuple(e for e in self.compile_errors if "missing declaration" in e.lower() or "undeclared" in e.lower())


@dataclass(frozen=True)
class Action:
    kind: str
    reason: str
    needs_provider: bool = False
    details: dict = field(default_factory=dict)


class EscalationPolicy:
    """Configurable thresholds over the P8.4 default table. Thresholds are calibrated, not magic: the
    defaults are a starting rule set and every threshold is a constructor argument."""

    def __init__(self, *, attempts_before_analysis: int = 2, attempts_before_release: int = 6,
                 profile_confidence_threshold: float = 0.5, disable_after_failures: int = 3,
                 min_cost_to_disable: float = 5.0):
        self.attempts_before_analysis = attempts_before_analysis
        self.attempts_before_release = attempts_before_release
        self.profile_confidence_threshold = profile_confidence_threshold
        self.disable_after_failures = disable_after_failures
        self.min_cost_to_disable = min_cost_to_disable
        self.rule_stats: dict[str, dict] = {kind: {"investigations": 0, "improvements": 0, "cost": 0.0}
                                            for kind in RULE_KINDS}
        self.disabled: dict[str, str] = {}

    # ------------------------------------------------------------------ observations (P8.7)

    def observe(self, kind: str, *, improved: bool, cost: float, count: bool = True) -> None:
        """Record one escalation's outcome for a rule (count=False: a later verification of an already
        counted escalation). A rule that consistently adds cost without any verified progress is
        disabled with a recorded reason."""
        stats = self.rule_stats.setdefault(kind, {"investigations": 0, "improvements": 0, "cost": 0.0})
        if count:
            stats["investigations"] += 1
        stats["cost"] += cost
        if improved:
            stats["improvements"] += 1
        if (kind not in self.disabled and stats["investigations"] >= self.disable_after_failures
                and stats["improvements"] == 0 and stats["cost"] >= self.min_cost_to_disable):
            self.disabled[kind] = (f"{stats['investigations']} escalations, {stats['cost']:.1f}s total cost, "
                                   f"no verified improvement: rule disabled (P8.7)")

    def disabled_reasons(self) -> dict:
        return dict(self.disabled)

    def enable(self, kind: str) -> None:
        self.disabled.pop(kind, None)
        self.rule_stats.pop(kind, None)

    # ------------------------------------------------------------------ classification

    @staticmethod
    def _counts(mismatch: dict | None) -> dict:
        return (mismatch or {}).get("counts") or {}

    def register_only(self, inputs: EscalationInputs) -> bool:
        """Structurally close with only register/stack differences: a permutation problem, not an
        analysis problem."""
        counts = self._counts(inputs.mismatch)
        return (any(counts.get(k, 0) for k in _REGISTER_KEYS)
                and all(not counts.get(k, 0) for k in _STRUCTURAL_KEYS))

    def structural(self, inputs: EscalationInputs) -> bool:
        """Insertions/deletions/reorders or control-flow uncertainty: the instruction stream itself
        disagrees, so targeted evidence beats blind permutation."""
        counts = self._counts(inputs.mismatch)
        return (any(counts.get(k, 0) for k in _STRUCTURAL_KEYS)
                or inputs.control_flow_uncertain or inputs.jump_table_uncertain)

    # ------------------------------------------------------------------ decision

    def decide(self, inputs: EscalationInputs) -> Action:
        """The P8.4 default table, in order, skipping rules that were auto-disabled (P8.7)."""

        def take(kind: str, reason: str, **details) -> Action | None:
            if kind in self.disabled:
                return None
            return Action(kind, reason, **details)

        if inputs.twin_available or inputs.promising_source:
            if (a := take("twin_source", "an exact twin or a promising compatible source exists: try that "
                          "source before any deeper analysis")):
                return a
        if not inputs.m2c_tried:
            if (a := take("m2c_draft", "no m2c draft has been tried under a valid context: generate and "
                          "compile the draft before starting analysis")):
                return a
        if inputs.missing_declarations:
            if (a := take("context_repair", "repeated compile errors from missing declarations: repair the "
                          "context/prototypes before mutating code", needs_provider=False,
                          details={"declarations": list(inputs.missing_declarations)})):
                return a
        register_only = self.register_only(inputs)
        structural = self.structural(inputs)
        if register_only and not structural:
            if (a := take("permutation", "code is structurally close with register/stack differences: "
                          "guided permutation, not analysis")):
                return a
        if structural and (inputs.attempts >= self.attempts_before_analysis
                           or inputs.control_flow_uncertain or inputs.jump_table_uncertain):
            if (a := take("investigate_function", "control-flow, boundary or jump-table uncertainty persists "
                          "after valid attempts: a targeted Ghidra investigation",
                          needs_provider=True, details={"attempts": inputs.attempts})):
                return a
        if inputs.conflicting_cross_function_evidence:
            if (a := take("investigate_neighbourhood", "conflicting structure/type evidence across related "
                          "functions: a small neighbourhood investigation", needs_provider=True)):
                return a
        if (inputs.compiler_profile_confidence is not None
                and inputs.compiler_profile_confidence < self.profile_confidence_threshold):
            if (a := take("profile_calibration", "the compiler profile remains doubtful across multiple "
                          "functions: bounded profile calibration")):
                return a
        if inputs.data_literal_mismatch:
            if (a := take("data_literal_check", "data/literal differences remain after the code aligns: "
                          "focus on the unit's data/literal checks rather than generic code analysis")):
                return a
        if (not inputs.recent_progress and not inputs.new_evidence_expected
                and inputs.attempts >= self.attempts_before_release):
            if (a := take("release_cooldown", "no progress and no new evidence expected: release with "
                          "structured notes and a cooldown")):
                return a
        return Action("none", "no escalation warranted on the current inputs")


# ---------------------------------------------------------------------------- investigations (P8.5)

EVIDENCE_FACETS = ("instructions", "blocks", "calls", "references", "types")


def build_investigation(bundle: EvidenceBundle, *, question: str, function_key: str,
                        budget_total: Budget, wall_seconds: float, worker_seconds: float,
                        stopping_condition: str = "", status: str = "completed",
                        candidate_changes: list | None = None) -> Investigation:
    """Assemble the P8.5 contract from one evidence bundle. Facts carry evidence ids; hypotheses come
    from the provider's adapted proposals (bounded); the budget and stopping condition are explicit."""
    evidence = bundle.functions.get(function_key)
    facts: list[Fact] = []
    unresolved: list[str] = []
    consumed = Budget(wall_seconds=max(0.0, wall_seconds), worker_seconds=max(0.0, worker_seconds),
                      token_limit=budget_total.token_limit)
    if evidence is None:
        return Investigation(investigation_id=str(uuid.uuid4()), question=question, function_key=function_key,
                             snapshot=bundle.snapshot, provider=bundle.provider, engine=bundle.engine,
                             correlation_group=bundle.correlation_group, coverage={}, unresolved=["no evidence "
                             f"was produced for {function_key!r}"], budget_consumed=consumed,
                             budget_total=budget_total, stopping_condition="no evidence; nothing to test",
                             status=status)
    views = evidence.views
    if evidence.entry is not None:
        facts.append(Fact(f"{function_key}:entry", f"function entry at {evidence.entry:#x}",
                          addresses=[evidence.entry]))
    if evidence.classification in ("thunk", "external"):
        facts.append(Fact(f"{function_key}:classification",
                          f"classified as {evidence.classification}: no C draft is produced", []))
    for i, ins in enumerate((views.get("instructions") or {}).get("items", [])[:500]):
        facts.append(Fact(f"{function_key}:instructions:{i}",
                          f"instruction @ {ins.get('address', 0):#x}: {ins.get('asm', '')}",
                          addresses=[ins.get("address", 0)]))
    for i, call in enumerate((views.get("calls") or {}).get("items", [])[:100]):
        if call.get("indirect") and call.get("target") is None:
            facts.append(Fact(f"{function_key}:calls:{i}",
                              f"indirect call @ {call.get('address', 0):#x} with no resolved destination",
                              addresses=[call.get("address", 0)]))
            unresolved.append(f"the indirect call at {call.get('address', 0):#x} (vtable, function "
                              f"pointer or switch dispatch?)")
        else:
            facts.append(Fact(f"{function_key}:calls:{i}",
                              f"call @ {call.get('address', 0):#x} -> {call.get('name') or call.get('target')}",
                              addresses=[call.get("address", 0)]))
    for i, ref in enumerate((views.get("references") or {}).get("items", [])[:200]):
        facts.append(Fact(f"{function_key}:references:{i}",
                          f"{ref.get('kind', 'data')} reference @ {ref.get('address', 0):#x} to "
                          f"{ref.get('target', 0):#x}{(': ' + str(ref.get('value', ''))) if ref.get('value') else ''}",
                          addresses=[ref.get("address", 0)]))
    for i, t in enumerate((views.get("types") or {}).get("items", [])[:100]):
        facts.append(Fact(f"{function_key}:types:{i}",
                          f"recovered {t.get('name', '')}: {t.get('signature', '')} "
                          f"(origin {t.get('origin', 'decompiler')}, complete={t.get('complete', False)})", []))

    hypotheses: list[Hypothesis] = []
    for i, p in enumerate((views.get("proposals") or {}).get("items", [])[:8]):
        evidence_ids = list(p.get("evidence_ids") or [])
        contradicting = [f for f in p.get("contradicting") or []]
        hypotheses.append(Hypothesis(statement=str(p.get("hypothesis") or "unlabelled observation"),
                                     supporting=evidence_ids, contradicting=contradicting, rank=i))
    for i, open_question in enumerate(unresolved[:3]):
        hypotheses.append(Hypothesis(statement=open_question, supporting=[],
                                     contradicting=[], rank=10 + i))

    if stopping_condition == "":
        stopping_condition = ("stop when the expected discriminating observation is seen in the standard "
                              "compile/diff fast path, when the stated unknown is resolved, or when the "
                              "budget is exhausted")
    return Investigation(investigation_id=str(uuid.uuid4()), question=question, function_key=function_key,
                         snapshot=bundle.snapshot, provider=bundle.provider, engine=bundle.engine,
                         correlation_group=bundle.correlation_group, facts=facts,
                         coverage=dict(evidence.coverage or {}), unresolved=unresolved,
                         hypotheses=hypotheses, candidate_changes=list(candidate_changes or []),
                         budget_consumed=consumed, budget_total=budget_total, stopping_condition=stopping_condition,
                         status=status)


def changes_from(bundle: EvidenceBundle, function_key: str, snapshot: str) -> list[CandidateChange]:
    """Minimal candidate changes from the provider's adapted proposals, each with an expected
    discriminating compile/diff observation. Nothing is edited here (P8.6/P7.6)."""
    evidence = bundle.functions.get(function_key)
    if evidence is None:
        return []
    out: list[CandidateChange] = []
    for p in (evidence.views.get("proposals") or {}).get("items", [])[:8]:
        for change in p.get("requested_changes") or []:
            kind = str(change.get("kind", "observe"))
            expected = {
                "type": "compiling the proposed type change removes the operand/width mismatch at the "
                        "affected instructions in the diff",
                "classify": "no code change is expected: the diff is unchanged and the function is "
                            "recorded as classified",
                "observe": "no source change is proposed: the observation only narrows the search",
            }.get(kind, "the standard compile/diff path shows whether the change moves the score")
            out.append(CandidateChange(kind=kind, target=str(change.get("target", "")),
                                       before=str(change.get("before") or ""),
                                       after=str(change.get("after") or ""),
                                       expected_observation=expected,
                                       confidence=float(p.get("confidence") or 0.0),
                                       snapshot=snapshot))
    return out[:8]


def run_investigation(broker, function_key: str, question: str, evidence_refs, budget: Budget | None = None,
                      *, snapshot: str = "current", requester: str = "", cancel=None) -> Investigation:
    """Fetch focused evidence through the broker (coalesced, bounded, snapshotted) and assemble the
    P8.5 investigation contract. `evidence_refs` may pin existing evidence; unknown refs are re-fetched."""
    budget_total = (budget or Budget()).clone()
    start = time.monotonic()
    bundle = broker.get_function_evidence(function_key, EVIDENCE_FACETS, snapshot, budget_total.clone(),
                                          requester=requester, cancel=cancel)
    wall = time.monotonic() - start
    status = "completed"
    if bundle.status == "budget_exceeded":
        status = "budget_exceeded"
    elif bundle.status in ("cancelled", "failed", "retryable", "denied"):
        status = "cancelled" if bundle.status == "cancelled" else "failed"
    changes = changes_from(bundle, function_key, snapshot)
    investigation = build_investigation(bundle, question=question, function_key=function_key,
                                        budget_total=budget_total, wall_seconds=wall,
                                        worker_seconds=bundle.worker_seconds, status=status,
                                        candidate_changes=changes)
    for ref in evidence_refs or []:  # caller-pinned evidence stays visible as a fact
        investigation.facts.append(Fact(f"{function_key}:ref:{ref}",
                                        f"caller-supplied evidence reference {ref}", []))
    # versioned portfolio: candidates attach to the evidence/snapshot that produced them (P8.6)
    portfolio = broker.portfolio(function_key)
    for change in investigation.candidate_changes:
        portfolio.propose(function_key, bundle.provider, snapshot,
                          source=(change.after or change.target or ""),
                          provenance={"engine": bundle.engine, "correlation_group": bundle.correlation_group,
                                      "investigation_id": investigation.investigation_id,
                                      "question": question},
                          change=change)
    return investigation


# ---------------------------------------------------------------------------- candidate portfolio (P8.6)

class CandidatePortfolio:
    """Candidate sources for one function, each bound to the evidence and snapshot that produced it. An
    analysis-assisted candidate becomes current best only after current-snapshot scoring; accepted only
    after the required verification. Context changes demote old-snapshot currents to historical."""

    def __init__(self, function_key: str):
        self.function_key = function_key
        self.records: list[CandidateRecord] = []

    def propose(self, function_key: str, provider: str, snapshot: str, *, source: str,
                provenance: dict | None = None, change: CandidateChange | None = None) -> CandidateRecord:
        record = CandidateRecord(function_key=function_key, provider=provider, snapshot=snapshot,
                                 source=source, provenance=dict(provenance or {}), change=change)
        self.records.append(record)
        return record

    def mark_current(self, record: CandidateRecord, snapshot: str, *, scored: bool) -> None:
        """An analysis-assisted candidate becomes the current best only after current-snapshot scoring."""
        if not scored:
            raise BrokerError("a candidate can only become current after it is scored under the current "
                              "snapshot (P8.6)")
        record.status = "current"
        record.scored_under = snapshot
        for other in self.records:
            if other is not record and other.status == "current":
                other.status = "historical"

    def accept(self, record: CandidateRecord) -> None:
        """Accepted only after the required verification (the caller supplies it)."""
        record.status = "accepted"
        for other in self.records:
            if other is not record and other.status in ("current", "proposed"):
                other.status = "historical"

    def on_snapshot_change(self, new_snapshot: str) -> int:
        """Context changed mid-investigation: retain everything as historical evidence; old-snapshot
        currents can never be applied as current again."""
        changed = 0
        for record in self.records:
            if record.snapshot != new_snapshot and record.status in ("current", "proposed"):
                record.status = "historical"
                changed += 1
        return changed

    def current(self) -> CandidateRecord | None:
        return next((r for r in self.records if r.status == "current"), None)

    def apply_change(self, change: CandidateChange, current_snapshot: str) -> str:
        """Applying a proposed change requires it to be valid under the current snapshot (A-06)."""
        if change.snapshot != current_snapshot:
            raise StaleEvidenceError(
                f"candidate change for {self.function_key!r} was produced under snapshot "
                f"{change.snapshot!r}; the current snapshot is {current_snapshot!r}. Re-resolve the "
                f"relevant facts through refresh_evidence before applying anything.")
        return (f"apply {change.kind} change to {change.target!r} through decomp.wtf's protected edit "
                f"path; expected observation: {change.expected_observation}")

    def to_dict(self) -> list[dict]:
        out = []
        for r in self.records:
            d = {"function_key": r.function_key, "provider": r.provider, "snapshot": r.snapshot,
                 "source": r.source, "provenance": r.provenance, "status": r.status,
                 "scored_under": r.scored_under}
            if r.change is not None:
                d["change"] = {k: getattr(r.change, k) for k in
                               ("kind", "target", "before", "after", "expected_observation", "confidence", "snapshot")}
            out.append(d)
        return out
