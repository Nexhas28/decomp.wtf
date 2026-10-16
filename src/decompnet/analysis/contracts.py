"""Data contracts for the analysis broker (Phase 8, P8.1-P8.7).

Everything crossing the broker boundary is versioned data: bounded views with evidence references
(never "all raw provider tools"), explicit provider/engine provenance, correlation groups so that two
routes over the same engine/profile/input are never counted as independent confirmation, and focused
investigation contracts. The provider interface itself lives in :mod:`decompnet.analysis.broker` so
that the REA adapter (PR-25) implements exactly that surface.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Bundle / evidence statuses. "retryable" means a shared session died and the request must be re-issued
# explicitly; "denied" means a quota or admission gate refused it; "budget_exceeded" means the request
# exceeded its explicit limits and nothing is treated as a complete success.
BUNDLE_STATUSES = ("ok", "partial", "failed", "cancelled", "budget_exceeded", "retryable", "denied")
# Session lifecycle: closed -> starting -> ready -> draining -> closed, with failed/cancelled exits.
SESSION_STATES = ("closed", "starting", "ready", "draining", "failed", "cancelled")
FACETS = ("pseudocode", "instructions", "blocks", "calls", "references", "types", "pcode")


class BrokerError(RuntimeError):
    pass


class TargetError(BrokerError):
    """The requested function/module was never admitted through register_target."""


class CapabilityError(BrokerError):
    """No configured provider admits the target/profile, or the manifest rejects it."""


class ProviderError(BrokerError):
    """A provider failed (session open, fetch, refresh). The broker never silently switches engines."""


class StaleEvidenceError(BrokerError):
    """Evidence/candidates were produced under a different snapshot and cannot be applied as current."""


@dataclass(frozen=True)
class Snapshot:
    """decomp.wtf snapshot identity: content version evidence and candidates are valid for. A context
    change produces a new snapshot id; evidence from older snapshots stays historical, never current."""

    id: str

    def __str__(self) -> str:
        return self.id


@dataclass
class Budget:
    """Explicit cost limits for one evidence/investigation request (P8.1/P8.5)."""

    wall_seconds: float = 60.0
    worker_seconds: float = 30.0
    token_limit: int = 8000

    def clone(self) -> "Budget":
        return Budget(self.wall_seconds, self.worker_seconds, self.token_limit)

    def spend(self, wall: float = 0.0, worker: float = 0.0) -> "Budget":
        self.wall_seconds = max(0.0, self.wall_seconds - wall)
        self.worker_seconds = max(0.0, self.worker_seconds - worker)
        return self

    def exhausted(self) -> bool:
        return self.wall_seconds <= 0.0 or self.worker_seconds <= 0.0

    def to_dict(self) -> dict:
        return {"wall_seconds": self.wall_seconds, "worker_seconds": self.worker_seconds,
                "token_limit": self.token_limit}


@dataclass(frozen=True)
class EvidenceRef:
    """A bounded, indirect reference to retained evidence (a dossier row, a facet stream, a cache entry).
    Detailed data is reached through these, not by exposing raw provider tools to every agent."""

    ref: str
    facet: str = ""
    size_hint: int = 0
    summary: str = ""

    def __str__(self) -> str:
        return self.ref


@dataclass(frozen=True)
class Capability:
    """A provider's advertised capability manifest: hosts, architectures/modes, formats and facets it
    claims. The per-target decision is admission (capabilities()), not this table alone."""

    hosts: tuple = ("any",)
    processors: tuple = ()
    modes: tuple = ()
    bits: tuple = ()
    endians: tuple = ("big", "little")
    formats: tuple = ("any",)
    facets: tuple = FACETS
    max_functions_per_request: int = 64
    live_sessions: bool = False
    notes: str = ""


@dataclass(frozen=True)
class CapabilityResult:
    """Admission of one target/profile against a provider, with reasons either way."""

    supported: bool
    provider: str = ""
    reasons: tuple = ()  # admission warnings or rejection reasons
    profile: dict | None = None  # the admitted/pinned profile identity when supported


@dataclass
class ProviderRequest:
    """One serialised request handed to a provider session. The broker builds these; providers only read
    them (and the cancel event)."""

    request_id: str
    kind: str  # function | neighbourhood
    function_key: str
    functions: tuple = ()  # function names to fetch (primary plus, for neighbourhoods, related ones)
    facets: tuple = ()
    snapshot: str = "current"
    budget: Budget = field(default_factory=Budget)
    limits: dict = field(default_factory=dict)
    profile: dict = field(default_factory=dict)  # admitted profile identity
    inputs: dict = field(default_factory=dict)  # provider bindings: artifact path, module, mapping...
    requester: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class FunctionEvidence:
    """Normalised, bounded evidence for one function, with provenance (P8.2/P8.3)."""

    function_key: str
    provider: str
    engine: str
    correlation_group: str
    snapshot: str
    status: str
    coverage: dict = field(default_factory=dict)
    views: dict = field(default_factory=dict)  # facet -> bounded view (lists/dicts, truncated)
    refs: dict = field(default_factory=dict)  # facet/name -> EvidenceRef
    diagnostics: list = field(default_factory=list)
    classification: str = "unknown"
    entry: int | None = None


@dataclass
class ProviderResult:
    """What a provider returns for one request. Statuses: ok, partial, failed, cancelled, timeout,
    budget_exceeded, denied, retryable."""

    status: str
    functions: dict = field(default_factory=dict)  # function_key -> FunctionEvidence
    refs: dict = field(default_factory=dict)
    diagnostics: list = field(default_factory=list)
    denied_reason: str = ""
    wall_seconds: float = 0.0
    worker_seconds: float = 0.0
    reused: bool = False


@dataclass
class EvidenceBundle:
    """The broker's response to get_function_evidence / get_neighbourhood_evidence."""

    request_id: str
    requester: str
    function_keys: tuple
    snapshot: str
    provider: str
    engine: str
    correlation_group: str
    status: str
    functions: dict = field(default_factory=dict)
    refs: dict = field(default_factory=dict)
    diagnostics: list = field(default_factory=list)
    reused: bool = False
    from_cache: bool = False
    wall_seconds: float = 0.0
    worker_seconds: float = 0.0
    budget: Budget = field(default_factory=Budget)
    shared: bool = False  # True when this result was coalesced onto another caller's request

    def ok(self) -> bool:
        return self.status in ("ok", "partial")


# ---------------------------------------------------------------------------- investigation (P8.5)

@dataclass
class Fact:
    """One observed fact with its evidence id and addresses."""

    evidence_id: str
    statement: str
    addresses: list = field(default_factory=list)


@dataclass
class Hypothesis:
    """A ranked explanation with the evidence ids supporting and contradicting it."""

    statement: str
    supporting: list = field(default_factory=list)
    contradicting: list = field(default_factory=list)
    rank: int = 0


@dataclass
class CandidateChange:
    """A minimal source/context change to test, plus the discriminating observation expected from the
    standard compile/diff fast path."""

    kind: str  # source | context | type | classify | observe
    target: str
    before: str = ""
    after: str = ""
    expected_observation: str = ""
    confidence: float = 0.0
    snapshot: str = "current"


@dataclass
class Investigation:
    """The P8.5 investigation contract: question, observed facts with evidence ids, coverage and
    unresolved questions, ranked hypotheses, minimal candidate changes, expected discriminating
    observations, budget consumed and a stopping condition."""

    investigation_id: str
    question: str
    function_key: str
    snapshot: str
    provider: str
    engine: str
    correlation_group: str
    facts: list = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    unresolved: list = field(default_factory=list)
    hypotheses: list = field(default_factory=list)
    candidate_changes: list = field(default_factory=list)
    budget_consumed: Budget = field(default_factory=Budget)
    budget_total: Budget = field(default_factory=Budget)
    stopping_condition: str = ""
    status: str = "completed"  # completed | budget_exceeded | cancelled | historical

    def ranked_hypotheses(self) -> list[Hypothesis]:
        return sorted(self.hypotheses, key=lambda h: (h.rank, -len(h.supporting)))


# ---------------------------------------------------------------------------- candidates (P8.6)

@dataclass
class CandidateRecord:
    """A candidate source attached to the evidence and snapshot that produced it. An analysis-assisted
    candidate becomes current best only after current-snapshot scoring; it becomes accepted only after
    the required verification."""

    function_key: str
    provider: str
    snapshot: str
    source: str
    provenance: dict = field(default_factory=dict)
    status: str = "proposed"  # proposed | current | accepted | historical
    scored_under: str | None = None
    change: CandidateChange | None = None

    def bind(self, snapshot: str) -> "CandidateRecord":
        self.snapshot = snapshot
        return self


# ---------------------------------------------------------------------------- measurement (P8.7)

@dataclass
class EscalationMetrics:
    """One escalation's measured value: cold/warm, wall and worker time, evidence reused vs new, agent
    tokens, compile attempts before/after, and whether it produced a verified improvement or resolved
    the stated unknown."""

    escalation_id: str
    function_key: str
    action: str
    cold_start: bool
    wall_seconds: float = 0.0
    worker_seconds: float = 0.0
    evidence_reused: bool = False
    evidence_new: bool = False
    additional_tokens: int = 0
    compiles_before: int = 0
    compiles_after: int = 0
    verified_improvement: bool = False
    resolved_unknown: bool = False
    outcome: str = ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "escalation_id", "function_key", "action", "cold_start", "wall_seconds", "worker_seconds",
            "evidence_reused", "evidence_new", "additional_tokens", "compiles_before", "compiles_after",
            "verified_improvement", "resolved_unknown", "outcome")}


# ---------------------------------------------------------------------------- durable evidence (18.5)

#: Serialised form of the Section 18.5 evidence record (bump when its JSON shape changes).
EVIDENCE_SCHEMA_VERSION = 1

#: Always attached: provider-observed facts never stand for complete knowledge of runtime behaviour.
SCOPE_LIMITATION = "coverage is scoped to the producing analysis; it is not complete knowledge of runtime behaviour"


@dataclass(frozen=True)
class EvidenceRecord:
    """The durable form of one function's evidence (plan 18.5). Facets map to ``{"status", "payload_ref"}``
    or ``{"status": "unavailable", "reason"}``; payloads live content-addressed beside the record. Target
    content (strings, names, pseudocode) is untrusted data, never instructions, whatever the route."""

    evidence_id: str
    function_key: str
    target_digest: str
    provider_route: str
    underlying_engine: str
    engine_version: str
    analysis_profile_digest: str
    correlation_group: str
    snapshot: str
    status: str
    facets: dict = field(default_factory=dict)
    observations: list = field(default_factory=list)
    inferences: list = field(default_factory=list)
    unknowns: list = field(default_factory=list)
    limitations: list = field(default_factory=lambda: [SCOPE_LIMITATION])
    untrusted_target_content: bool = True
    original_provider_evidence_ref: str | None = None
    classification: str = "unknown"
    entry: str | None = None
    schema_version: int = EVIDENCE_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "schema_version", "evidence_id", "function_key", "target_digest", "provider_route",
            "underlying_engine", "engine_version", "analysis_profile_digest", "correlation_group", "snapshot",
            "status", "facets", "observations", "inferences", "unknowns", "limitations",
            "untrusted_target_content", "original_provider_evidence_ref", "classification", "entry")}

    @classmethod
    def from_dict(cls, d: dict) -> "EvidenceRecord":
        if int(d.get("schema_version", 1)) > EVIDENCE_SCHEMA_VERSION:
            raise ValueError(f"evidence record schema {d['schema_version']} is newer than this version reads")
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
