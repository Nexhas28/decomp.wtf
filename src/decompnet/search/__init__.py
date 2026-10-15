"""Guided, syntax-aware permuter search (plan P5.4-P5.6).

Mismatch-routed operator weights (P5.4), a bounded diverse candidate portfolio (P5.5) and
evaluation dedup without diversity loss (P5.6), plus the versioned search-policy spec that a
distributed worker can carry (P5.8 groundwork). The authoritative byte judge still decides every
acceptance; this package only steers proposals and reuse.
"""

from .dedup import EvalCache, EvalOutcome
from .policy import SEARCH_POLICY, SearchPolicy, SearchPolicyError, parse_policy
from .portfolio import Candidate, Portfolio, structure_key
from .routing import ROUTING_TABLES, RoutingDecision, route
from .run import (SearchResumeError, SearchRun, StepResult, checkpoint_run, new_run, resume_run,
                  step)

__all__ = [
    "SEARCH_POLICY", "SearchPolicy", "SearchPolicyError", "parse_policy",
    "ROUTING_TABLES", "RoutingDecision", "route",
    "EvalCache", "EvalOutcome",
    "Candidate", "Portfolio", "structure_key",
    "SearchRun", "StepResult", "SearchResumeError", "new_run", "step", "checkpoint_run", "resume_run",
]
