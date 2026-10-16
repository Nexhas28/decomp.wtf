"""Function ID known-library hints (P10.2, PR-27).

Ghidra Function ID recognises compiled library functions from processor-specific databases. A hit here is
candidate identity evidence, never proof of an exact decompilation:

* candidates carry processor/mode and corpus versions explicitly;
* ambiguous or conflicting hits are preserved, not collapsed — the shortlist keeps every variant (B-01);
* names/types are adapted ONLY through controlled candidate construction (:func:`build_candidate`); a
  hit is never marked matched and never renamed into authoritative symbols;
* compile-and-verify through the coordinator remains mandatory: this module returns candidates, never
  verdicts (``matched``/``is_match`` are always False here);
* the real Ghidra lane requires analyzeHeadless plus a Java runtime; without them every query skips with
  an explicit reason (:func:`decompnet.retrieval.bsim.toolchain_available`). The logic here is
  implemented and tested against a fake in-memory/JSON index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .bsim import toolchain_available
from .registry import (Admission, ReferenceEntry, Registry, RetrievalConfig, admit_retrieval)

FID_SCHEMA = 1

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ $\t*&]*$")


class FidError(RuntimeError):
    """A Function ID hit cannot be turned into a candidate (controlled construction rejected it)."""


@dataclass(frozen=True)
class FunctionIdHit:
    """One known-library hit. Processor/mode and corpus versions are explicit (P10.2.2); ``matched`` is
    an invariant False — a hit is never proof of an exact decompilation (P10.2)."""

    hit_id: str
    name: str                # the known-library function name (a hint, not an authoritative symbol)
    library: str
    repository: str
    version: str             # repository/corpus version the hit was built from (B-04)
    corpus_version: str      # the Function ID database (compiler-variant) version
    processor: str
    mode: str
    signature: str           # digest of the function bytes the hit matches
    entry_id: str = ""       # registry entry the hit resolves to ("" = no source mapping, B-03)
    profile: str = ""        # provider profile the hit was built under (B-04)
    target_digest: str = ""  # target identity the hit was built against (B-04)
    confidence: float = 1.0
    matched: bool = False    # invariant: Function ID never marks anything matched

    def to_dict(self) -> dict:
        return {"hit_id": self.hit_id, "name": self.name, "library": self.library,
                "repository": self.repository, "version": self.version,
                "corpus_version": self.corpus_version, "processor": self.processor, "mode": self.mode,
                "signature": self.signature, "entry_id": self.entry_id, "profile": self.profile,
                "target_digest": self.target_digest, "confidence": self.confidence,
                "matched": self.matched}

    @classmethod
    def from_dict(cls, d: dict) -> "FunctionIdHit":
        return cls(hit_id=str(d["hit_id"]), name=str(d.get("name", "")),
                   library=str(d.get("library", "")), repository=str(d.get("repository", "")),
                   version=str(d.get("version", "")), corpus_version=str(d.get("corpus_version", "")),
                   processor=str(d.get("processor", "mips")), mode=str(d.get("mode", "")),
                   signature=str(d.get("signature", "")), entry_id=str(d.get("entry_id", "")),
                   profile=str(d.get("profile", "")), target_digest=str(d.get("target_digest", "")),
                   confidence=float(d.get("confidence", 1.0)),
                   matched=bool(d.get("matched", False)))


@dataclass(frozen=True)
class FidCandidate:
    """A candidate built from a hit. ``is_match`` is an invariant False: compile-and-verify through the
    coordinator decides, and retrieval never marks anything matched (P10.2.6)."""

    hit: FunctionIdHit
    entry: ReferenceEntry | None = None
    suggested_name: str | None = None   # adapted ONLY through controlled candidate construction
    suggested_types: tuple = ()
    adaptation: str = ""
    requires_verification: bool = True
    is_match: bool = False              # invariant: never set by retrieval

    @property
    def entry_id(self) -> str:
        return self.entry.entry_id if self.entry else ""

    @property
    def source_available(self) -> bool:
        return bool(self.entry is not None and self.entry.usable())

    @property
    def source_location(self) -> str:
        """The reusable source path only when it is actually usable; never a fabricated one (B-03)."""
        return self.entry.source_location if self.source_available else ""

    @property
    def evidence_only(self) -> bool:
        """True when the hit has no usable source mapping: investigation evidence, not reusable C."""
        return not self.source_available


def build_candidate(hit: FunctionIdHit, entry: ReferenceEntry | None = None, *,
                    suggested_name: str | None = None, suggested_types: tuple = (),
                    adaptation: str = "") -> FidCandidate:
    """Controlled candidate construction — the ONLY way a hit gains names/types (P10.2.5).

    Names and types are validated here; anything invalid raises :class:`FidError` instead of being
    silently adapted. The result is a candidate with ``is_match=False``: a hit is never marked matched
    and never renamed into authoritative symbols.
    """
    if suggested_name is not None:
        if not isinstance(suggested_name, str) or not suggested_name.strip():
            raise FidError(f"invalid suggested name {suggested_name!r}: must be a non-empty identifier")
        if not _IDENT.fullmatch(suggested_name.strip()):
            raise FidError(f"invalid suggested name {suggested_name!r}: only identifiers are adapted, "
                           "never arbitrary text")
    types = tuple(suggested_types or ())
    if not all(isinstance(t, str) and _TYPE.fullmatch(t) for t in types):
        raise FidError(f"invalid suggested types {types!r}: names/types are adapted only through "
                       "validated candidate construction")
    return FidCandidate(hit=hit, entry=entry,
                        suggested_name=suggested_name.strip() if suggested_name else None,
                        suggested_types=types, adaptation=adaptation,
                        requires_verification=True, is_match=False)


def adapt_candidate(candidate: FidCandidate, *, suggested_name: str | None = None,
                    suggested_types: tuple = ()) -> FidCandidate:
    """A controlled name/type adaptation of an existing candidate: returns a NEW candidate, never mutates
    the hit and never marks anything matched (P10.2.5)."""
    return build_candidate(candidate.hit, candidate.entry, suggested_name=suggested_name,
                           suggested_types=suggested_types, adaptation=candidate.adaptation)


@dataclass
class FidResult:
    """One lookup's answer. Ambiguity is preserved: every matching hit becomes a candidate, conflicting
    names included (B-01)."""

    status: str             # ok | empty | disabled | denied | skipped
    reason: str = ""
    candidates: list = field(default_factory=list)
    hits: tuple = ()
    stale: int = 0
    ambiguous: bool = False

    def ok(self) -> bool:
        return self.status in ("ok", "empty")


class FunctionIdIndex:
    """A fake in-memory Function ID index (the real Ghidra FID database goes through the gated lane).

    Keyed by (processor, mode, signature) with every hit retained: :meth:`lookup` returns ALL hits for a
    signature rather than picking one, so ambiguity and per-variant corpus versions survive candidate
    selection (B-01). :meth:`validate` marks hits stale when the registry entry they were built from
    changed version/target/profile — an index/corpus version change invalidates the signature and its
    source metadata (B-04).
    """

    def __init__(self, registry: Registry | None = None):
        self.registry = registry
        self.schema = FID_SCHEMA
        self._hits: dict[str, FunctionIdHit] = {}
        self._by_key: dict[tuple, list] = {}
        self._stale: dict[str, str] = {}
        self.lookups = 0

    # ------------------------------------------------------------ lifecycle

    def add(self, hit: FunctionIdHit) -> str:
        """Index one hit; returns its id. All hits for a signature are retained (ambiguity preserved)."""
        self._hits[hit.hit_id] = hit
        self._by_key.setdefault((hit.processor, hit.mode, hit.signature), []).append(hit.hit_id)
        return hit.hit_id

    def remove(self, hit_id: str) -> None:
        hit = self._hits.pop(hit_id, None)
        if hit is not None:
            key = (hit.processor, hit.mode, hit.signature)
            self._by_key.get(key, []).remove(hit_id)
        self._stale.pop(hit_id, None)

    def hit(self, hit_id: str) -> FunctionIdHit | None:
        return self._hits.get(hit_id)

    def stale(self, hit_id: str) -> bool:
        return hit_id in self._stale

    def stale_reason(self, hit_id: str) -> str:
        """Why a hit was invalidated ("" when it is current) — diagnostics for B-04."""
        return self._stale.get(hit_id, "")

    def validate(self, registry: Registry | None = None) -> int:
        """Recompute staleness against the registry (B-04): a hit with a source mapping is valid only
        while the mapped entry is admitted, not a holdout, and its version, target identity and profile
        still match what the hit was built under. Hits without a source mapping stay valid evidence
        (B-03)."""
        registry = registry or self.registry
        self._stale = {}
        for hit in self._hits.values():
            if not hit.entry_id:
                continue  # no source mapping: investigation evidence, not stale (B-03)
            entry = registry.get(hit.entry_id) if registry else None
            if entry is None:
                self._stale[hit.hit_id] = "no registry entry for the hit's source mapping"
            elif entry.holdout:
                self._stale[hit.hit_id] = "holdout entry: must not be indexed for retrieval"
            elif hit.version != entry.version:
                self._stale[hit.hit_id] = f"corpus version changed ({hit.version} -> {entry.version})"
            elif hit.target_digest != entry.target_identity:
                self._stale[hit.hit_id] = "target identity changed"
            elif hit.profile != entry.profile:
                self._stale[hit.hit_id] = f"provider profile changed ({hit.profile} -> {entry.profile})"
        return len(self._stale)

    # ------------------------------------------------------------ retrieval

    def lookup(self, signature: str, processor: str, mode: str, *, confidence_floor: float = 0.0,
               validate_first: bool = True) -> FidResult:
        """ALL hits for (processor, mode, signature), stale ones excluded. Ambiguity is preserved: hits
        with conflicting names are all returned, never collapsed into a guess (B-01)."""
        stale = self.validate() if validate_first else len(self._stale)
        self.lookups += 1
        candidates: list = []
        hits = tuple(self._hits[h] for h in self._by_key.get((processor, mode, signature), ())
                     if h not in self._stale and self._hits[h].confidence >= confidence_floor)
        for hit in hits:
            entry = (self.registry.get(hit.entry_id)
                     if self.registry and hit.entry_id else None)
            candidates.append(build_candidate(hit, entry))
        reason = ""
        if stale:
            reason = f"{stale} stale hit(s) excluded: index/corpus version or provider profile changed"
        status = "ok" if candidates else "empty"
        return FidResult(status=status, reason=reason, candidates=candidates, hits=hits, stale=stale,
                         ambiguous=len({c.hit.name for c in candidates}) > 1)

    # ------------------------------------------------------------ JSON fixtures

    def to_json(self, *, indent: int = 2) -> str:
        import json

        return json.dumps({"schema": self.schema,
                           "hits": [h.to_dict() for h in self._hits.values()],
                           "stale": dict(sorted(self._stale.items()))}, indent=indent)

    @classmethod
    def from_json(cls, text: str, registry: Registry | None = None) -> "FunctionIdIndex":
        import json

        data = json.loads(text)
        index = cls(registry)
        index.schema = int(data.get("schema", FID_SCHEMA))
        for row in data.get("hits", []):
            index.add(FunctionIdHit.from_dict(row))
        index._stale = dict(data.get("stale") or {})
        return index


# ---------------------------------------------------------------------------- the gated lane

class FidLane:
    """The corpus-gated Function ID entry point: disabled unless ``function_id_retrieval`` is enabled AND
    admission passes; the real lane also requires analyzeHeadless + Java, and any remote query needs the
    explicit ``external_queries`` opt-in (P10.5). Returns candidates, never verdicts."""

    def __init__(self, index: FunctionIdIndex, registry: Registry, config: RetrievalConfig):
        self.index = index
        self.registry = registry
        self.config = config

    def admitted(self) -> Admission:
        return admit_retrieval(self.config, self.registry)

    def available(self) -> tuple[bool, str]:
        return toolchain_available()

    def lookup(self, signature: str, processor: str, mode: str, *, external: bool = False) -> FidResult:
        if not self.config.function_id_retrieval:
            return FidResult(status="disabled", reason="function_id_retrieval is disabled (default off)")
        admission = self.admitted()
        if not admission.allowed:
            return FidResult(status="denied", reason="; ".join(admission.reasons))
        if external and not self.config.external_queries:
            return FidResult(status="denied", reason="no external Function ID queries by default: set "
                                                     "external_queries=True explicitly")
        ok, reason = self.available()
        if not ok:
            return FidResult(status="skipped", reason=reason)
        return self.index.lookup(signature, processor, mode)
