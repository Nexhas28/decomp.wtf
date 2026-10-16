"""Optional Function ID and BSim retrieval (plan Phase 10, PR-27/PR-28).

Both routes are corpus-gated and DEFAULT DISABLED: ``RetrievalConfig.function_id_retrieval`` and
``RetrievalConfig.bsim_retrieval`` are off by default, no external query or upload ever happens without
the explicit ``RetrievalConfig.external_queries`` opt-in, and the admission gate refuses to enable a
route that has no usable, permitted reference corpus. Retrieval never touches match state: Function ID
and BSim return candidates only, and compile-and-verify through the coordinator stays mandatory.

Modules:

* :mod:`decompnet.retrieval.registry` — the versioned reference-source registry (P10.1), holdout
  markers and leakage checks (P10.5);
* :mod:`decompnet.retrieval.fid` — Function ID known-library hints (P10.2);
* :mod:`decompnet.retrieval.bsim` — the local BSim pilot: versioned signatures, sidecar source/compiler
  metadata, bounded filtered shortlists and the route-ordering helper (P10.3/P10.4).
"""

from .bsim import (BSIM_FEATURE_NAMES, SIGNATURE_SCHEMA, BsimCandidate, BsimIndex, BsimLane,
                   BsimResult, BsimSignature, RouteStep, SidecarEntry, enabled_routes,
                   normalise_instructions, route_plan, signature_similarity, toolchain_available)
from .fid import (FID_SCHEMA, FidCandidate, FidError, FidLane, FidResult, FunctionIdHit,
                  FunctionIdIndex, adapt_candidate, build_candidate)
from .registry import (DEFAULT_ROUTE_ORDER, REGISTRY_SCHEMA, ROUTE_KINDS, Admission, Leakage,
                       ReferenceEntry, Registry, RetrievalConfig, admit_retrieval, digest,
                       normalise_location)

__all__ = [
    # registry (P10.1/P10.5)
    "REGISTRY_SCHEMA", "DEFAULT_ROUTE_ORDER", "ROUTE_KINDS", "Admission", "Leakage", "ReferenceEntry",
    "Registry", "RetrievalConfig", "admit_retrieval", "digest", "normalise_location",
    # function ID (P10.2)
    "FID_SCHEMA", "FidCandidate", "FidError", "FidLane", "FidResult", "FunctionIdHit",
    "FunctionIdIndex", "adapt_candidate", "build_candidate",
    # BSim (P10.3/P10.4)
    "BSIM_FEATURE_NAMES", "SIGNATURE_SCHEMA", "BsimCandidate", "BsimIndex", "BsimLane", "BsimResult",
    "BsimSignature", "RouteStep", "SidecarEntry", "enabled_routes", "normalise_instructions",
    "route_plan", "signature_similarity", "toolchain_available",
]
