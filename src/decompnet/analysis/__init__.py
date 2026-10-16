"""The analysis broker (Phase 8, P8.1-P8.7): one narrow internal contract that admits targets, coalesces
equivalent requests, owns bounded provider sessions, escalates only when it is likely to prevent wasted
search, and measures whether analysis actually helped.

Public surface: :class:`~decompnet.analysis.broker.Broker`,
:class:`~decompnet.analysis.broker.Provider` (the exact interface the PR-25 REA adapter implements),
:class:`~decompnet.analysis.broker.DirectGhidraProvider`,
:class:`~decompnet.analysis.escalation.EscalationPolicy` and the data contracts in
:mod:`decompnet.analysis.contracts`.
"""

from .broker import (Broker, DirectGhidraProvider, MetricsLedger, Provider, ProviderError,
                     independent_sources)
from .contracts import (BUNDLE_STATUSES, SESSION_STATES, BrokerError, Budget, CandidateChange,
                        CandidateRecord, Capability, CapabilityError, CapabilityResult, EscalationMetrics,
                        EvidenceBundle, EvidenceRef, Fact, FunctionEvidence, Hypothesis, Investigation,
                        ProviderRequest, ProviderResult, Snapshot, StaleEvidenceError, TargetError)
from .escalation import (Action, CandidatePortfolio, EscalationInputs, EscalationPolicy, RULE_KINDS,
                         build_investigation, changes_from, run_investigation)
from .rea import ReaProvider, available as rea_available

__all__ = [
    "Broker", "DirectGhidraProvider", "MetricsLedger", "Provider", "ProviderError", "independent_sources",
    "BUNDLE_STATUSES", "SESSION_STATES", "BrokerError", "Budget", "CandidateChange", "CandidateRecord",
    "Capability", "CapabilityError", "CapabilityResult", "EscalationMetrics", "EvidenceBundle",
    "EvidenceRef", "Fact", "FunctionEvidence", "Hypothesis", "Investigation", "ProviderRequest",
    "ProviderResult", "Snapshot", "StaleEvidenceError", "TargetError",
    "Action", "CandidatePortfolio", "EscalationInputs", "EscalationPolicy", "RULE_KINDS",
    "build_investigation", "changes_from", "run_investigation",
    "ReaProvider", "rea_available",
]
