"""Local BSim pilot (P10.3/P10.4, PR-28).

BSim retrieves similar functions from normalised data/control-flow features. BSim is explicitly NOT an
exact-match judge: its features intentionally OMIT constant values, register names and data types, so
functions that differ only in constants/types legitimately rank close, and acceptance is the
coordinator's compile-and-verify path — never this module (B-02, P10.5).

* versioned signatures generated from admitted profiles (registry-gated, :data:`SIGNATURE_SCHEMA`);
* a sidecar table links each candidate to source and compiler metadata — the index itself never stores
  complete source (B-03, plan G02);
* retrieval returns a bounded shortlist filtered by usable source/profile and reranked with
  project-specific features; candidates without usable source stay investigation evidence, never
  fictitious reusable C;
* :func:`route_plan` is the documented route-ordering helper — exact twins -> known-library hints ->
  lightweight local similarity -> BSim for unresolved cases with a useful reference corpus — with
  configurable ordering (P10.4);
* no remote queries or uploads by default: external/shared indexes require the explicit
  ``RetrievalConfig.external_queries`` opt-in (P10.5).

The real Ghidra BSim lane requires analyzeHeadless plus a Java runtime; without them every query skips
with an explicit reason (:func:`toolchain_available`). The logic here is implemented and tested against
a fake in-memory/JSON index.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .registry import Admission, Registry, RetrievalConfig, admit_retrieval

SIGNATURE_SCHEMA = 1

#: The signature features BSim keeps: normalised data/control-flow only. Constant values, register
#: names and data types are deliberately OMITTED (G02): BSim is not an exact-match judge.
BSIM_FEATURE_NAMES = ("mnemonic_histogram", "length", "blocks", "branch_density", "loops", "calls")


def toolchain_available() -> tuple[bool, str]:
    """Whether the real BSim/Function ID lane can run: Ghidra's analyzeHeadless plus a Java runtime."""
    try:
        from ..forks import ghidra

        if ghidra.available():
            return True, "Ghidra (analyzeHeadless) and a Java runtime are available"
        return (False, "the real BSim/Function ID lane cannot run: Ghidra (analyzeHeadless) and/or a "
                       f"Java runtime are not installed on this host (ghidra {ghidra.provider_version()})")
    except Exception as e:
        return False, f"the real BSim/Function ID lane cannot run: {e}"


# ---------------------------------------------------------------------------- signature model

def normalise_instructions(lines) -> tuple[str, ...]:
    """The signature-relevant view of formatted instructions: mnemonics only. Constants, register names
    and data types are deliberately omitted — the very reason BSim ranks near-twins close (B-02)."""
    return tuple(line.split(None, 1)[0].lower() for line in lines if line.strip())


@dataclass(frozen=True)
class BsimSignature:
    """A versioned BSim signature from one admitted profile (P10.3.2)."""

    sig_id: str
    processor: str
    mode: str
    profile: str                 # admitted toolchain/profile identity
    program_digest: str          # identity of the analyzed program the signature came from
    histogram: tuple             # ((mnemonic, count), ...) — no constants/registers/types
    length: int
    blocks: int = 1
    branches: int = 0
    loops: int = 0
    calls: int = 0
    schema: int = SIGNATURE_SCHEMA

    @classmethod
    def from_lines(cls, sig_id: str, lines, *, processor: str = "mips", mode: str = "",
                   profile: str = "", program_digest: str = "", blocks: int = 1, branches: int = 0,
                   loops: int = 0, calls: int = 0, schema: int = SIGNATURE_SCHEMA) -> "BsimSignature":
        ops = normalise_instructions(lines)
        histogram = tuple(sorted(Counter(ops).items()))
        return cls(sig_id=sig_id, processor=processor, mode=mode, profile=profile,
                   program_digest=program_digest, histogram=histogram, length=len(ops),
                   blocks=blocks, branches=branches, loops=loops, calls=calls, schema=schema)

    def cosine(self, other: "BsimSignature") -> float:
        a = dict(self.histogram)
        b = dict(other.histogram)
        dot = sum(v * b.get(k, 0) for k, v in a.items())
        na = sum(v * v for v in a.values()) ** 0.5
        nb = sum(v * v for v in b.values()) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    def to_dict(self) -> dict:
        return {"sig_id": self.sig_id, "processor": self.processor, "mode": self.mode,
                "profile": self.profile, "program_digest": self.program_digest,
                "histogram": [list(pair) for pair in self.histogram], "length": self.length,
                "blocks": self.blocks, "branches": self.branches, "loops": self.loops,
                "calls": self.calls, "schema": self.schema}

    @classmethod
    def from_dict(cls, d: dict) -> "BsimSignature":
        return cls(sig_id=str(d["sig_id"]), processor=str(d.get("processor", "mips")),
                   mode=str(d.get("mode", "")), profile=str(d.get("profile", "")),
                   program_digest=str(d.get("program_digest", "")),
                   histogram=tuple((str(k), int(v)) for k, v in d.get("histogram") or ()),
                   length=int(d.get("length", 0)), blocks=int(d.get("blocks", 1)),
                   branches=int(d.get("branches", 0)), loops=int(d.get("loops", 0)),
                   calls=int(d.get("calls", 0)), schema=int(d.get("schema", SIGNATURE_SCHEMA)))


def signature_similarity(a: BsimSignature, b: BsimSignature) -> float:
    """0..1 over normalised data/control-flow features only. NO constants, register names or data types
    participate, so near-twins that differ in constants/types rank close (B-02)."""
    if not a.length or not b.length:
        return 0.0

    def ratio(x: float, y: float) -> float:
        return min(x, y) / max(x, y) if max(x, y) > 0 else 1.0

    hist = a.cosine(b)
    flow = (ratio(a.blocks, b.blocks) + ratio(a.branches / a.length, b.branches / b.length)
            + ratio(a.loops, b.loops) + ratio(a.calls, b.calls)) / 4
    return 0.7 * hist + 0.3 * flow


# ---------------------------------------------------------------------------- sidecar table

@dataclass(frozen=True)
class SidecarEntry:
    """The sidecar reference table: each candidate's link to source and compiler metadata (P10.3.4).
    The index itself stores no complete source (B-03)."""

    candidate_key: str
    entry_id: str            # registry entry this candidate maps to ("" = no source mapping)
    source_location: str
    compiler_profile: str
    toolchain: str
    version: str             # corpus version at signature time (B-04)
    program_digest: str
    language: str = ""
    source_available: bool = True

    def to_dict(self) -> dict:
        return {"candidate_key": self.candidate_key, "entry_id": self.entry_id,
                "source_location": self.source_location, "compiler_profile": self.compiler_profile,
                "toolchain": self.toolchain, "version": self.version,
                "program_digest": self.program_digest, "language": self.language,
                "source_available": self.source_available}

    @classmethod
    def from_dict(cls, d: dict) -> "SidecarEntry":
        return cls(candidate_key=str(d.get("candidate_key", "")), entry_id=str(d.get("entry_id", "")),
                   source_location=str(d.get("source_location", "")),
                   compiler_profile=str(d.get("compiler_profile", "")),
                   toolchain=str(d.get("toolchain", "")), version=str(d.get("version", "")),
                   program_digest=str(d.get("program_digest", "")),
                   language=str(d.get("language", "")),
                   source_available=bool(d.get("source_available", True)))


# ---------------------------------------------------------------------------- candidates

@dataclass(frozen=True)
class BsimCandidate:
    """One retrieved function. ``is_match`` is an invariant False here: BSim never matches anything —
    the coordinator's compile-and-verify judge decides (B-02)."""

    key: str
    signature: BsimSignature
    sidecar: SidecarEntry
    similarity: float
    rank_score: float
    notes: tuple = ()
    evidence_only: bool = False   # no usable source: investigation evidence, not reusable C (B-03)
    is_match: bool = False        # invariant: retrieval never sets this

    @property
    def source_available(self) -> bool:
        return not self.evidence_only

    @property
    def source_location(self) -> str:
        """The reusable source path only when it is actually usable; never a fabricated one (B-03)."""
        return "" if self.evidence_only else self.sidecar.source_location


def _default_rerank(target: BsimSignature, candidate: BsimCandidate, similarity: float,
                    project_features: dict | None) -> tuple[float, tuple]:
    """Rerank with project-specific features (P10.3.5): profile match, prior verification, preferred
    toolchains and language — applied as visible bonuses, never as a similarity claim."""
    bonus, notes = 0.0, []
    pf = project_features or {}
    if candidate.sidecar.compiler_profile and candidate.sidecar.compiler_profile == target.profile:
        bonus += 0.03
        notes.append("compiler profile matches the target")
    if candidate.key in pf.get("verified_functions", ()):
        bonus += 0.02
        notes.append("previously verified source")
    if candidate.sidecar.toolchain in pf.get("preferred_toolchains", ()):
        bonus += 0.01
        notes.append("preferred toolchain")
    if pf.get("language") and candidate.sidecar.language and pf["language"] == candidate.sidecar.language:
        bonus += 0.01
        notes.append("source language matches")
    return similarity + bonus, tuple(notes)


# ---------------------------------------------------------------------------- the index

@dataclass
class BsimResult:
    """One query's answer: a bounded, filtered shortlist plus retained investigation evidence."""

    status: str            # ok | empty | disabled | denied | skipped
    reason: str = ""
    candidates: list = field(default_factory=list)      # bounded shortlist: usable source/profile only
    evidence_only: list = field(default_factory=list)   # retrieved, no usable source (B-03)
    excluded: list = field(default_factory=list)        # {"key", "similarity", "reason"}: filtered out
    similarity_calls: int = 0

    def ok(self) -> bool:
        return self.status in ("ok", "empty")


class BsimIndex:
    """A local in-memory BSim index (the real Ghidra BSim database goes through the gated lane).

    * versioned signatures from admitted profiles;
    * a sidecar table linking candidates to source and compiler metadata;
    * bounded shortlist retrieval filtered by usable source/profile and reranked with project-specific
      features (:meth:`query`, P10.3.5);
    * :meth:`validate` invalidates signatures whose schema, source or compiler metadata no longer
      matches the registry — an index/corpus version or profile change is diagnosably stale (B-04).
    """

    def __init__(self, registry: Registry | None = None, *, cold: bool = False, build_cost: float = 0.0):
        self.registry = registry
        self.cold = cold
        self.build_cost = build_cost
        self.schema = SIGNATURE_SCHEMA
        self._sigs: dict[str, BsimSignature] = {}
        self._sidecars: dict[str, SidecarEntry] = {}
        self._stale: dict[str, str] = {}
        self.queries = 0
        self.invalidations = 0

    # ------------------------------------------------------------ lifecycle

    def add(self, signature: BsimSignature, sidecar: SidecarEntry) -> None:
        self._sigs[signature.sig_id] = signature
        self._sidecars[signature.sig_id] = sidecar

    def remove(self, sig_id: str) -> None:
        self._sigs.pop(sig_id, None)
        self._sidecars.pop(sig_id, None)
        self._stale.pop(sig_id, None)

    def _check_policy(self) -> None:
        if self.schema != SIGNATURE_SCHEMA:
            self._stale.clear()
            for sig in self._sigs.values():
                self._stale[sig.sig_id] = (f"signature schema {sig.schema} predates "
                                           f"SIGNATURE_SCHEMA {SIGNATURE_SCHEMA}")
            self.schema = SIGNATURE_SCHEMA

    def validate(self, registry: Registry | None = None) -> int:
        """Recompute staleness against the registry (B-04): a signature is valid only while its schema is
        current, its sidecar maps to an admitted, non-holdout entry, and that entry's version, target
        identity and profile still match what the signature was built under."""
        registry = registry or self.registry
        self._check_policy()
        self._stale = {}
        for sig in self._sigs.values():
            if sig.schema != SIGNATURE_SCHEMA:
                self._stale[sig.sig_id] = "signature schema is stale"
                continue
            sidecar = self._sidecars.get(sig.sig_id)
            if sidecar is None:
                self._stale[sig.sig_id] = "missing sidecar source/compiler metadata"
                continue
            if not sidecar.entry_id:
                continue  # no source mapping: investigation evidence, not stale (B-03)
            entry = registry.get(sidecar.entry_id) if registry else None
            if entry is None:
                self._stale[sig.sig_id] = "no registry entry for the candidate's source mapping"
            elif entry.holdout:
                self._stale[sig.sig_id] = "holdout entry: must not be indexed for retrieval"
            elif sidecar.version != entry.version:
                self._stale[sig.sig_id] = f"corpus version changed ({sidecar.version} -> {entry.version})"
            elif sidecar.program_digest != entry.target_identity:
                self._stale[sig.sig_id] = "target identity changed"
            elif sidecar.compiler_profile != entry.profile:
                self._stale[sig.sig_id] = f"provider profile changed ({sidecar.compiler_profile} -> "
                f"{entry.profile})"
        self.invalidations += len(self._stale)
        return len(self._stale)

    def stale(self, sig_id: str) -> bool:
        return sig_id in self._stale

    def stale_reason(self, sig_id: str) -> str:
        """Why a signature was invalidated ("" when it is current) — diagnostics for B-04."""
        return self._stale.get(sig_id, "")

    def usable_count(self, registry: Registry | None = None) -> int:
        """Signatures whose source is usable and permitted under the registry (admission, B-06)."""
        registry = registry or self.registry
        self.validate(registry)
        count = 0
        for sig_id, sidecar in self._sidecars.items():
            if sig_id in self._stale:
                continue
            entry = registry.get(sidecar.entry_id) if registry else None
            if sidecar.source_available and entry is not None and entry.usable():
                count += 1
        return count

    # ------------------------------------------------------------ retrieval

    def query(self, target: BsimSignature, *, top: int = 8, floor: float = 0.0,
              project_features: dict | None = None, rerank=None) -> BsimResult:
        """Bounded shortlist retrieval (P10.3.5): compatible processor/mode, similarity floor, filter by
        usable source/profile, rerank with project-specific features. Candidates without usable source
        are retained as investigation evidence (B-03) but never enter the reusable shortlist."""
        self.validate()
        self.queries += 1
        rerank = rerank or _default_rerank
        result = BsimResult(status="ok")
        for sig in self._sigs.values():
            if target.processor and sig.processor != target.processor:
                continue
            if target.mode and sig.mode != target.mode:
                continue
            if sig.sig_id in self._stale:
                sidecar = self._sidecars.get(sig.sig_id)
                result.excluded.append({"key": sidecar.candidate_key if sidecar else sig.sig_id,
                                        "similarity": 0.0, "reason": self._stale[sig.sig_id]})
                continue
            sim = signature_similarity(target, sig)
            if sim < floor:
                continue
            result.similarity_calls += 1
            sidecar = self._sidecars[sig.sig_id]
            entry = (self.registry.get(sidecar.entry_id)
                     if self.registry and sidecar.entry_id else None)
            usable = (sidecar.source_available and entry is not None and entry.usable()
                      and not entry.holdout)
            if usable:
                candidate = BsimCandidate(key=sidecar.candidate_key, signature=sig, sidecar=sidecar,
                                          similarity=sim, rank_score=sim, evidence_only=False)
                rank, notes = rerank(target, candidate, sim, project_features)
                candidate = BsimCandidate(key=candidate.key, signature=candidate.signature,
                                          sidecar=sidecar, similarity=sim, rank_score=rank,
                                          notes=notes, evidence_only=False)
                result.candidates.append(candidate)
            else:
                result.evidence_only.append(BsimCandidate(
                    key=sidecar.candidate_key, signature=sig, sidecar=sidecar, similarity=sim,
                    rank_score=sim, evidence_only=True,
                    notes=("no usable source: investigation evidence only, never reusable C",)))
        result.candidates.sort(key=lambda c: (-c.rank_score, -c.similarity))
        result.candidates = result.candidates[:max(1, top)]
        result.evidence_only.sort(key=lambda c: -c.similarity)
        if not result.candidates and not result.evidence_only:
            result.status = "empty"
            result.reason = "no BSim candidates above the floor (or all are stale)"
        return result

    # ------------------------------------------------------------ JSON fixtures

    def to_json(self, *, indent: int = 2) -> str:
        import json

        return json.dumps({"schema": self.schema, "cold": self.cold, "build_cost": self.build_cost,
                           "signatures": [s.to_dict() for s in self._sigs.values()],
                           "sidecars": {sig_id: s.to_dict() for sig_id, s in self._sidecars.items()},
                           "stale": dict(sorted(self._stale.items()))}, indent=indent)

    @classmethod
    def from_json(cls, text: str, registry: Registry | None = None) -> "BsimIndex":
        import json

        data = json.loads(text)
        index = cls(registry, cold=bool(data.get("cold", False)),
                    build_cost=float(data.get("build_cost", 0.0)))
        index.schema = int(data.get("schema", SIGNATURE_SCHEMA))
        for row in data.get("signatures", []):
            sig = BsimSignature.from_dict(row)
            index._sigs[sig.sig_id] = sig
        for sig_id, row in (data.get("sidecars") or {}).items():
            index._sidecars[sig_id] = SidecarEntry.from_dict(row)
        index._stale = dict(data.get("stale") or {})
        return index


# ---------------------------------------------------------------------------- the gated lane

class BsimLane:
    """The corpus-gated local BSim entry point: disabled unless ``bsim_retrieval`` is enabled AND
    admission passes; the real lane also requires analyzeHeadless + Java, and any remote query needs the
    explicit ``external_queries`` opt-in (P10.5). Returns candidates, never verdicts."""

    def __init__(self, index: BsimIndex, registry: Registry, config: RetrievalConfig):
        self.index = index
        self.registry = registry
        self.config = config

    def admitted(self) -> Admission:
        return admit_retrieval(self.config, self.registry, bsim=self.index)

    def available(self) -> tuple[bool, str]:
        return toolchain_available()

    def query(self, target: BsimSignature, *, top: int = 8, floor: float = 0.0,
              project_features: dict | None = None, external: bool = False) -> BsimResult:
        if not self.config.bsim_retrieval:
            return BsimResult(status="disabled", reason="bsim_retrieval is disabled (default off)")
        admission = self.admitted()
        if not admission.allowed:
            return BsimResult(status="denied", reason="; ".join(admission.reasons))
        if external and not self.config.external_queries:
            return BsimResult(status="denied", reason="no external BSim queries by default: set "
                                                      "external_queries=True explicitly")
        ok, reason = self.available()
        if not ok:
            return BsimResult(status="skipped", reason=reason)
        return self.index.query(target, top=top, floor=floor, project_features=project_features)


# ---------------------------------------------------------------------------- route ordering (P10.4)

@dataclass(frozen=True)
class RouteStep:
    """One step of the retrieval route plan, with why it is enabled or skipped."""

    name: str
    enabled: bool
    reason: str

    def to_dict(self) -> dict:
        return {"name": self.name, "enabled": self.enabled, "reason": self.reason}


def route_plan(config: RetrievalConfig, registry: Registry | None = None,
               bsim: BsimIndex | None = None) -> list:
    """The documented route-ordering helper (P10.4).

    Default order: **exact twins -> cheap known-library (Function ID) hints -> lightweight local
    similarity -> BSim** for unresolved cases with a useful reference corpus. The order is configurable
    via ``RetrievalConfig.route_order`` (validated at construction). Function ID and BSim steps are
    enabled only when their flag is on AND the corpus admission gate passes; with both flags off (the
    default) the plan falls through to the existing similarity routes without touching FID/BSim.

    Do not launch a cold Ghidra analysis solely to query BSim for every trivial function: a BSim step is
    never enabled while its cold index build exceeds ``RetrievalConfig.bsim_max_cold_cost``.
    """
    admission = admit_retrieval(config, registry or Registry(), bsim=bsim)
    denied = set()
    for reason in admission.reasons:
        if "function_id_retrieval requires" in reason:
            denied.add("function_id")
        if "bsim_retrieval requires" in reason or "BSim index" in reason or "cold BSim" in reason:
            denied.add("bsim")
    steps: list = []
    for name in config.route_order:
        if name == "exact_twins":
            steps.append(RouteStep(name, True, "existing exact-twin fast path"))
        elif name == "function_id":
            if config.function_id_retrieval and name not in denied:
                steps.append(RouteStep(name, True, "known-library hints: enabled and corpus-admitted"))
            elif not config.function_id_retrieval:
                steps.append(RouteStep(name, False, "function_id_retrieval is disabled (default off)"))
            else:
                steps.append(RouteStep(name, False,
                                       next((r for r in admission.reasons
                                             if "function_id_retrieval requires" in r), "not admitted")))
        elif name == "lightweight_similarity":
            steps.append(RouteStep(name, True, "existing lightweight local similarity"))
        elif name == "bsim":
            if config.bsim_retrieval and name not in denied:
                steps.append(RouteStep(name, True, "local BSim: enabled and corpus-admitted"))
            elif not config.bsim_retrieval:
                steps.append(RouteStep(name, False, "bsim_retrieval is disabled (default off)"))
            else:
                steps.append(RouteStep(name, False,
                                       next((r for r in admission.reasons if "bsim" in r), "not admitted")))
    return steps


def enabled_routes(plan: list) -> list:
    """The enabled step names of a :func:`route_plan`, in order (diagnostics and tests)."""
    return [step.name for step in plan if step.enabled]
