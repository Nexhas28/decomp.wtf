"""Reference-source registry (P10.1, P10.5): the corpus every retrieval index is admitted against.

Every indexed reference is described exactly once: repository/version, source location, toolchain/profile,
target identity (content digest), provenance and usage restrictions. Function ID and BSim retrieval are
corpus-gated: the admission gate (:func:`admit_retrieval`) refuses to enable either route unless the
registry contains usable, permitted source, and no remote query or upload ever happens without an
explicit configuration opt-in (``RetrievalConfig.external_queries``, default False).

The registry is versioned (schema + a mutation counter) and serializable to JSON. ``save``/``load`` are
the explicit persistence API; wiring them into the database (plan Section 18.6/18.7, "reference-source
records and retrieval index versions") is a Lead follow-up and deliberately not done here — the API is
stable so that wiring does not change the contract.

Holdouts (P10.5): an entry can carry a ``holdout`` marker so benchmark answers never enter a retrieval
index, and :meth:`Registry.leakage_check` reports any answer that is accidentally present in the corpus.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

REGISTRY_SCHEMA = 1

#: The retrieval route ordering from P10.4: exact twins -> cheap known-library hints -> lightweight
#: local similarity -> BSim for unresolved cases with a useful reference corpus.
DEFAULT_ROUTE_ORDER = ("exact_twins", "function_id", "lightweight_similarity", "bsim")
ROUTE_KINDS = ("exact_twins", "function_id", "lightweight_similarity", "bsim")

#: A usage restriction that removes an entry from every retrieval shortlist (binary reference only).
_SOURCE_DENIED = "source-denied"


def digest(obj) -> str:
    """The canonical content digest of a JSON-serialisable object (registry/index cache identity)."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()


def normalise_location(location: str) -> str:
    """A comparable form of a source location (paths and URLs): stripped, de-dotted, trailing slash off."""
    loc = location.strip()
    if loc.startswith(("http://", "https://", "file://")):
        return loc.rstrip("/")
    try:
        loc = str(Path(loc))
    except Exception:
        pass
    return loc.rstrip("/") or loc


@dataclass(frozen=True)
class ReferenceEntry:
    """One admitted reference: repository/version, source, toolchain/profile, target identity,
    provenance and usage restrictions (P10.1)."""

    entry_id: str
    repository: str
    version: str                # repository/corpus version the entry was built from
    source_location: str        # permitted source location (path or URL); "" = no accessible source
    toolchain: str
    profile: str                # toolchain/profile identity the artifact was built under
    target_identity: str        # digest of the built artifact this entry describes
    processor: str = "mips"
    mode: str = ""
    provenance: str = ""        # how the entry was obtained and admitted
    usage_restrictions: tuple = ()  # e.g. ("source-denied", "no-redistribution", "internal-only")
    source_available: bool = True   # whether usable C source is accessible (False = binary reference)
    source_digest: str = ""         # digest of the referenced source, when known (leakage checks, B-05)
    holdout: bool = False           # P10.5: benchmark holdout answer; never indexed for retrieval

    def usable(self) -> bool:
        """Whether this entry may feed retrieval: source is accessible, permitted and not a holdout."""
        return self.source_available and not self.holdout and _SOURCE_DENIED not in self.usage_restrictions

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id, "repository": self.repository, "version": self.version,
            "source_location": self.source_location, "toolchain": self.toolchain, "profile": self.profile,
            "target_identity": self.target_identity, "processor": self.processor, "mode": self.mode,
            "provenance": self.provenance, "usage_restrictions": list(self.usage_restrictions),
            "source_available": self.source_available, "source_digest": self.source_digest,
            "holdout": self.holdout,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReferenceEntry":
        return cls(entry_id=str(d["entry_id"]), repository=str(d.get("repository", "")),
                   version=str(d.get("version", "")), source_location=str(d.get("source_location", "")),
                   toolchain=str(d.get("toolchain", "")), profile=str(d.get("profile", "")),
                   target_identity=str(d.get("target_identity", "")),
                   processor=str(d.get("processor", "mips")), mode=str(d.get("mode", "")),
                   provenance=str(d.get("provenance", "")),
                   usage_restrictions=tuple(d.get("usage_restrictions") or ()),
                   source_available=bool(d.get("source_available", True)),
                   source_digest=str(d.get("source_digest", "")),
                   holdout=bool(d.get("holdout", False)))


@dataclass(frozen=True)
class Leakage:
    """A benchmark answer accidentally present in the retrieval corpus (P10.5/B-05)."""

    entry_id: str
    answer_id: str
    kind: str      # source_digest | target_digest | source_location
    value: str
    note: str = ""

    def to_dict(self) -> dict:
        return {"entry_id": self.entry_id, "answer_id": self.answer_id, "kind": self.kind,
                "value": self.value, "note": self.note}


class Registry:
    """The versioned reference corpus.

    Mutations bump ``revision``; :meth:`identity` is a digest of the schema and the entries — the cache
    key retrieval indexes validate against, so an index/corpus change is diagnosably stale (B-04).
    """

    def __init__(self, entries=(), *, name: str = "", schema: int = REGISTRY_SCHEMA):
        self.schema = int(schema)
        self.name = name
        self._entries: dict[str, ReferenceEntry] = {}
        self._revision = 0
        for entry in entries:  # bulk construction is one revision, not one per entry
            self._entries[entry.entry_id] = entry

    # ------------------------------------------------------------ mutation (bumps revision)

    def add(self, entry: ReferenceEntry) -> str:
        """Admit one entry; returns its id. Re-adding an id replaces the entry and bumps the revision."""
        self._entries[entry.entry_id] = entry
        self._revision += 1
        return entry.entry_id

    def replace(self, entry: ReferenceEntry) -> None:
        """Replace an admitted entry (a corpus update, e.g. a new version): revision bump, identity change."""
        if entry.entry_id not in self._entries:
            raise KeyError(f"no registry entry {entry.entry_id!r} to replace")
        self._entries[entry.entry_id] = entry
        self._revision += 1

    def remove(self, entry_id: str) -> None:
        if self._entries.pop(entry_id, None) is not None:
            self._revision += 1

    # ------------------------------------------------------------ queries

    @property
    def revision(self) -> int:
        return self._revision

    def get(self, entry_id: str) -> ReferenceEntry | None:
        return self._entries.get(entry_id)

    def entries(self) -> dict:
        return dict(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, entry_id) -> bool:
        return entry_id in self._entries

    def __iter__(self):
        return iter(self._entries.values())

    def usable_entries(self, *, processor: str | None = None, mode: str | None = None) -> list:
        """Entries that may feed retrieval: accessible, permitted, not holdout, optionally matching the
        target's processor/mode."""
        out = []
        for entry in self._entries.values():
            if not entry.usable():
                continue
            if processor is not None and entry.processor != processor:
                continue
            if mode is not None and entry.mode != mode:
                continue
            out.append(entry)
        return out

    def identity(self) -> str:
        """Content digest of schema + entries: an index built under one registry identity is diagnosably
        stale under another (B-04)."""
        rows = sorted((e.to_dict() for e in self._entries.values()), key=lambda d: d["entry_id"])
        return digest({"schema": self.schema, "name": self.name, "entries": rows})

    # ------------------------------------------------------------ holdout leakage (P10.5)

    def leakage_check(self, answers: dict) -> list:
        """Flag answers accidentally present in the *indexed* corpus.

        ``answers`` maps an answer id to its identities, e.g.
        ``{"q1": {"source_digest": "...", "target_digest": "...", "source_location": "..."}}``. Any
        non-holdout entry whose source digest, target identity or normalised source location matches an
        answer is reported; holdout-marked entries are correctly separated and do not count as leakage.
        """
        found: list = []
        for entry in self._entries.values():
            if entry.holdout:
                continue  # correctly separated from the corpus, never a leak
            for answer_id, identity in answers.items():
                if entry.source_digest and entry.source_digest == identity.get("source_digest", ""):
                    found.append(Leakage(entry.entry_id, answer_id, "source_digest", entry.source_digest,
                                         "the answer's source is in the corpus: retrieval would credit "
                                         "an indexed copy of the answer"))
                if (entry.target_identity and
                        entry.target_identity == identity.get("target_digest", "")):
                    found.append(Leakage(entry.entry_id, answer_id, "target_digest", entry.target_identity,
                                         "the answer's built artifact is in the corpus"))
                if (entry.source_location and identity.get("source_location") and
                        normalise_location(entry.source_location)
                        == normalise_location(identity.get("source_location", ""))):
                    found.append(Leakage(entry.entry_id, answer_id, "source_location",
                                         entry.source_location, "the answer's source location is indexed"))
        return found

    # ------------------------------------------------------------ persistence (Lead wires the DB)

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize the whole registry (schema, name, revision, entries)."""
        return json.dumps({"schema": self.schema, "name": self.name, "revision": self._revision,
                           "entries": [e.to_dict() for e in self._entries.values()]}, indent=indent)

    @classmethod
    def from_json(cls, text: str) -> "Registry":
        """Rebuild a registry from :meth:`to_json` output; a foreign/older schema is surfaced, not guessed."""
        data = json.loads(text)
        registry = cls(name=str(data.get("name", "")), schema=int(data.get("schema", REGISTRY_SCHEMA)))
        registry._revision = int(data.get("revision", 0))
        for row in data.get("entries", []):
            entry = ReferenceEntry.from_dict(row)
            registry._entries[entry.entry_id] = entry
        return registry

    def save(self, path: Path | str) -> Path:
        """Explicit persistence API (Lead follow-up: store these rows/JSON in the DB instead of a file)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path | str) -> "Registry":
        """The matching explicit load API for :meth:`save`."""
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------- feature flags

@dataclass(frozen=True)
class RetrievalConfig:
    """Feature flags for the retrieval routes (plan Section 18.9).

    Both optional routes are DISABLED by default and no external query or upload ever happens without
    an explicit opt-in (P10.5): ``external_queries`` defaults to False and every lane refuses remote
    work unless it is set.
    """

    function_id_retrieval: bool = False
    bsim_retrieval: bool = False
    external_queries: bool = False
    route_order: tuple = DEFAULT_ROUTE_ORDER   # P10.4: configurable route ordering
    bsim_max_cold_cost: float | None = None    # None = no cap; set to refuse cold builds that cost too much

    def __post_init__(self):
        if not isinstance(self.route_order, tuple) or not self.route_order:
            raise ValueError("route_order must be a non-empty tuple of retrieval route names")
        seen: set = set()
        for name in self.route_order:
            if name not in ROUTE_KINDS:
                raise ValueError(f"unknown retrieval route {name!r}; allowed routes: {ROUTE_KINDS}")
            if name in seen:
                raise ValueError(f"duplicate retrieval route {name!r} in route_order")
            seen.add(name)

    def to_dict(self) -> dict:
        return {"function_id_retrieval": self.function_id_retrieval,
                "bsim_retrieval": self.bsim_retrieval, "external_queries": self.external_queries,
                "route_order": list(self.route_order), "bsim_max_cold_cost": self.bsim_max_cold_cost}

    @classmethod
    def from_dict(cls, d: dict) -> "RetrievalConfig":
        return cls(function_id_retrieval=bool(d.get("function_id_retrieval", False)),
                   bsim_retrieval=bool(d.get("bsim_retrieval", False)),
                   external_queries=bool(d.get("external_queries", False)),
                   route_order=tuple(d.get("route_order") or DEFAULT_ROUTE_ORDER),
                   bsim_max_cold_cost=d.get("bsim_max_cold_cost"))


@dataclass(frozen=True)
class Admission:
    """The admission gate's answer: allowed, with the reasons either way (P10.5/B-06)."""

    allowed: bool
    reasons: tuple = ()

    def ok(self) -> bool:
        return self.allowed


def admit_retrieval(config: RetrievalConfig, registry: Registry, *, bsim=None) -> Admission:
    """The corpus admission gate (B-06).

    A route that is requested but has no usable, permitted reference corpus is refused — the feature
    stays disabled rather than becoming a mandatory dependency. ``bsim`` (a BSim index, see
    :mod:`decompnet.retrieval.bsim`) adds its own checks: usable signatures and the cold-build cost
    cap. When neither route is enabled the gate is trivially satisfied: the feature is off by default,
    not an error.
    """
    reasons: list = []
    if config.function_id_retrieval and not registry.usable_entries():
        reasons.append("function_id_retrieval requires at least one usable, permitted registry entry "
                       "(accessible, permitted source); the registry has none — the feature stays disabled")
    if config.bsim_retrieval:
        if not registry.usable_entries():
            reasons.append("bsim_retrieval requires at least one usable, permitted registry entry; "
                           "the registry has none — the feature stays disabled")
        if bsim is not None:
            if not bsim.usable_count(registry):
                reasons.append("the BSim index has no signature with usable, permitted source — the "
                               "feature stays disabled")
            if (bsim.cold and config.bsim_max_cold_cost is not None
                    and bsim.build_cost > config.bsim_max_cold_cost):
                reasons.append(f"cold BSim index build cost {bsim.build_cost:g} exceeds the configured "
                               f"cap {config.bsim_max_cold_cost:g} — the feature stays disabled")
    if not config.function_id_retrieval and not config.bsim_retrieval:
        return Admission(True, ("no retrieval route enabled: the feature stays disabled by default",))
    return Admission(not reasons, tuple(reasons))
