"""The analysis broker (P8.1-P8.3, P8.7): one narrow internal contract over providers.

Responsibilities: target/profile admission (capabilities), cache reuse, request coalescing (only for
equivalent engine/profile/input), budgets, provider lifecycle with bounded session ownership, response
normalisation and measurement. Provider choice is explicit and recorded in provenance; a provider
failure is returned, never silently switched to another engine. Two routes over the same engine/profile/
input share one correlation group, so correlated evidence is never counted as independent confirmation.

Provider interface
------------------
The REA adapter (PR-25) must subclass :class:`Provider` and implement exactly this surface:

- ``name``: stable provider id, recorded in provenance (e.g. ``"rea-ghidra"``).
- ``engine``: the underlying engine identity (e.g. ``"ghidra-12.1.4"``); two providers with the same
  engine, profile and input are correlated, not independent.
- ``capabilities(target_profile, artifact=None) -> CapabilityResult``: admission against the manifest;
  reject with reasons, or return the pinned profile identity.
- ``open_session(session_key, profile) -> handle``: called once per session (state closed->starting);
  raise :class:`ProviderError` to fail the session. Must not re-enter the broker.
- ``close_session(session_key, handle)``: idempotent cleanup.
- ``fetch(session_key, handle, request, cancel) -> ProviderResult``: one serialised request. Must honour
  the ``cancel`` event promptly and return status ``"cancelled"``; return ``"budget_exceeded"`` when the
  request's :class:`~decompnet.analysis.contracts.Budget` is spent; raise :class:`ProviderError` to fail
  the session (all queued consumers then receive ``"retryable"``).
- ``refresh(session_key, scope, new_snapshot) -> int``: invalidate cached evidence for the scope after a
  snapshot/context change (returns the number of cache entries invalidated).
- ``flush(session_key, *, view_invalidated, reason)``: flushCache()-style metadata invalidation, or the
  stronger reset/reimport when the Program view is invalidated (overlay changes).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..forks import ghidra
from .contracts import (BUNDLE_STATUSES, BrokerError, Budget, CapabilityError, CapabilityResult,
                        EvidenceBundle, EvidenceRef, EscalationMetrics, FunctionEvidence, ProviderError,
                        ProviderRequest, ProviderResult, TargetError)
from .escalation import CandidatePortfolio, EscalationPolicy, run_investigation


def _identity(engine: str, profile: dict, digest: str, facets: tuple, functions: tuple, kind: str) -> str:
    """Request identity for coalescing: only equivalent (engine, profile, input identity, functions,
    facets, kind) requests share work (P8.2.6)."""
    payload = {"engine": engine, "profile": profile, "input": digest, "facets": sorted(facets),
               "functions": sorted(functions), "kind": kind}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _correlation_group(engine: str, profile: dict, digest: str) -> str:
    """Evidence identity shared by every route over the same engine/profile/input (P8.2.7)."""
    payload = {"engine": engine, "profile": profile, "input": digest}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def independent_sources(group_a: str, group_b: str) -> bool:
    """Two evidence groups are independent confirmation only when their engine/profile/input identity
    differs. Wrappers (direct vs REA) over the same engine never count twice."""
    return bool(group_a) and bool(group_b) and group_a != group_b


class Provider(ABC):
    """The broker's provider surface — the exact interface a REA adapter (PR-25) implements."""

    name: str = "provider"
    engine: str = "engine-unknown"

    @abstractmethod
    def capabilities(self, target_profile, artifact=None) -> CapabilityResult:
        """Admit the target/profile against the configured capability manifest (host/arch/mode/format)."""

    @abstractmethod
    def open_session(self, session_key: str, profile: dict):
        """Open one owned session. Raise ProviderError to fail startup; must not re-enter the broker."""

    @abstractmethod
    def close_session(self, session_key: str, handle) -> None:
        """Idempotent disposal of the owned processes/artifacts."""

    @abstractmethod
    def fetch(self, session_key: str, handle, request: ProviderRequest,
              cancel: threading.Event | None) -> ProviderResult:
        """Serve one serialised request. Honour cancel promptly; raise ProviderError to fail the session."""

    @abstractmethod
    def refresh(self, session_key: str, scope: str, new_snapshot: str) -> int:
        """Invalidate cached evidence for `scope` after a snapshot change; return the number invalidated."""

    @abstractmethod
    def flush(self, session_key: str, *, view_invalidated: bool, reason: str) -> None:
        """flushCache() on metadata changes; the stronger reset/reimport when view_invalidated."""


class DirectGhidraProvider(Provider):
    """The batched headless Ghidra route from PR-22/23, wrapped behind the provider surface. One
    analyze_object import serves every function of a module (the exporter reuses one DecompInterface per
    Program); caching, partial-failure handling and invalidation come from forks.ghidra."""

    name = "direct-ghidra"

    def __init__(self, cache_dir: Path | str):
        self.cache_dir = Path(cache_dir)
        self.engine = ghidra.provider_version()
        self._stems: dict[str, str] = {}
        self._ghidra_sessions = ghidra.SessionManager(cache_dir=self.cache_dir)

    # ------------------------------------------------------------ admission

    def capabilities(self, target_profile, artifact=None) -> CapabilityResult:
        data = b""
        if artifact is not None:
            p = Path(artifact)
            if p.exists():
                data = p.read_bytes()
        try:
            admitted = ghidra.admit(target_profile, data)
        except ghidra.AdmissionError as e:
            return CapabilityResult(False, self.name, (str(e),))
        return CapabilityResult(True, self.name, tuple(admitted.warnings), profile=admitted.identity())

    # ------------------------------------------------------------ sessions

    def open_session(self, session_key: str, profile: dict):
        if not ghidra.available():
            raise ProviderError("Ghidra (analyzeHeadless) is not installed: no analysis session can start")
        return {"profile": profile}

    def close_session(self, session_key: str, handle) -> None:
        self._stems.pop(session_key, None)

    # ------------------------------------------------------------ fetch

    def fetch(self, session_key: str, handle, request: ProviderRequest,
              cancel: threading.Event | None) -> ProviderResult:
        artifact = Path(request.inputs.get("artifact", ""))
        module = str(request.inputs.get("module", request.function_key.split("::", 1)[0]))
        self._stems.setdefault(session_key, artifact.stem)
        start = time.monotonic()
        try:
            manifest = ghidra.analyze_object(artifact, self.cache_dir,
                                             profile=request.inputs.get("profile"),
                                             timeout=request.budget.wall_seconds, facets=request.facets,
                                             cancel=cancel)
        except ghidra.GhidraUnavailable as e:
            raise ProviderError(f"ghidra unavailable: {e}") from e
        except ghidra.AdmissionError as e:
            return ProviderResult(status="denied", diagnostics=[str(e)], denied_reason=str(e))
        wall = time.monotonic() - start
        status = {"complete": "ok", "partial": "partial", "failed": "failed", "timeout": "timeout",
                  "cancelled": "cancelled"}.get(manifest.status, manifest.status)
        functions: dict[str, FunctionEvidence] = {}
        refs: dict[str, EvidenceRef] = {}
        limits = {**{"max_instructions": 200, "max_calls": 100, "max_refs": 200, "max_types": 100,
                     "max_blocks": 200, "max_pcode": 512, "max_pseudocode": 8000}, **request.limits}
        for name in request.functions:
            dossier = manifest.by_name(name)
            if dossier is None:
                functions[f"{module}::{name}"] = FunctionEvidence(
                    function_key=f"{module}::{name}", provider=self.name, engine=self.engine,
                    correlation_group="", snapshot=request.snapshot, status="failed",
                    diagnostics=[f"no dossier row for {name!r}"], coverage={f: "unknown" for f in ghidra.FACETS})
                continue
            key = f"{module}::{name}"
            evidence = self._dossier_evidence(dossier, key, request, limits)
            functions[key] = evidence
            refs[key] = EvidenceRef(ref=f"manifest:{manifest.cache_key}:{dossier.key}",
                                    facet="dossier", size_hint=len(evidence.views), summary=dossier.name)
        return ProviderResult(status=status, functions=functions, refs=refs,
                              diagnostics=list(manifest.diagnostics), wall_seconds=wall,
                              worker_seconds=wall, reused=manifest.from_cache)

    def _dossier_evidence(self, d: ghidra.FunctionDossier, key: str, request: ProviderRequest,
                          limits: dict) -> FunctionEvidence:
        views: dict[str, dict] = {}
        text = d.pseudocode.text
        views["pseudocode"] = {"status": d.pseudocode.status, "warnings": list(d.pseudocode.warnings),
                               "text": text[:limits["max_pseudocode"]],
                               "truncated": len(text) > limits["max_pseudocode"]}
        ins = [{"address": i.address, "size": i.size, "asm": i.mnemonic, "bytes": i.bytes_hex}
               for i in d.instructions[:limits["max_instructions"]]]
        if ins:
            views["instructions"] = {"items": ins, "truncated": len(d.instructions) > limits["max_instructions"]}
        blocks = [{"start": b.start, "end": b.end, "successors": b.successors}
                  for b in d.blocks[:limits["max_blocks"]]]
        if blocks or d.coverage.get("blocks") == "empty":
            views["blocks"] = {"items": blocks, "truncated": len(d.blocks) > limits["max_blocks"]}
        calls = [{"address": c.address, "target": c.target, "name": c.name, "indirect": c.indirect}
                 for c in d.calls[:limits["max_calls"]]]
        if calls or d.coverage.get("calls") == "empty":
            views["calls"] = {"items": calls, "truncated": len(d.calls) > limits["max_calls"]}
        refs = [{"address": r.address, "kind": r.kind, "target": r.target, "value": r.value}
                for r in d.references[:limits["max_refs"]]]
        if refs or d.coverage.get("references") == "empty":
            views["references"] = {"items": refs, "truncated": len(d.references) > limits["max_refs"]}
        types = [{"name": t.name, "signature": t.signature, "origin": t.origin, "complete": t.complete}
                 for t in d.types[:limits["max_types"]]]
        if types or d.coverage.get("types") == "empty":
            views["types"] = {"items": types, "truncated": len(d.types) > limits["max_types"]}
        proposals = []
        for p in ghidra.propose(d):
            proposals.append({"hypothesis": " ".join(p.hypotheses), "evidence_ids": list(p.evidence_ids),
                              "requested_changes": [dict(c) for c in p.requested_changes],
                              "confidence": p.confidence})
        views["proposals"] = {"items": proposals[:8], "truncated": len(proposals) > 8}
        return FunctionEvidence(function_key=key, provider=self.name, engine=self.engine,
                                correlation_group="", snapshot=request.snapshot,
                                status=d.coverage.get("pseudocode", "unknown"), coverage=dict(d.coverage),
                                views=views, diagnostics=[], classification=d.classification,
                                entry=d.entry)

    # ------------------------------------------------------------ invalidation

    def refresh(self, session_key: str, scope: str, new_snapshot: str) -> int:
        stem = self._stems.get(session_key) or scope.split("::", 1)[0]
        return ghidra.invalidate(self.cache_dir, stem)

    def flush(self, session_key: str, *, view_invalidated: bool, reason: str) -> None:
        stem = self._stems.get(session_key, "")
        self._ghidra_sessions.on_metadata_change(stem or session_key,
                                                 kind="overlay" if view_invalidated else "metadata",
                                                 reason=reason)


# ---------------------------------------------------------------------------- sessions (P8.3)

class Session:
    """One owned session: a key derived from artifact, module, mapping and analysis profile, with one
    serial request stream and explicit lifecycle states closed->starting->ready->draining->closed,
    failing or cancelling on the way."""

    def __init__(self, key: str, provider: Provider, profile: dict):
        self.key = key
        self.provider = provider
        self.profile = profile
        self.state = "closed"
        self.handle = None
        self.failure = ""
        self.pending = deque()
        self.active = None
        self.serial = threading.Lock()
        self.created_at = time.monotonic()
        self.last_used = self.created_at

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def start(self) -> bool:
        if self.state == "ready":
            return True
        self.state = "starting"
        try:
            self.handle = self.provider.open_session(self.key, self.profile)
        except ProviderError as e:
            self.state = "failed"
            self.failure = str(e)
            return False
        self.state = "ready"
        return True

    def cancel(self, reason: str) -> None:
        if self.state == "closed":
            return
        self.failure = reason
        self.state = "cancelled"
        self._dispose_handle()

    def close(self) -> None:
        self._dispose_handle()
        self.state = "closed"

    def _dispose_handle(self) -> None:
        if self.handle is not None:
            try:
                self.provider.close_session(self.key, self.handle)
            except Exception:
                pass
            self.handle = None

    def describe(self) -> dict:
        return {"key": self.key, "state": self.state, "failure": self.failure,
                "provider": self.provider.name, "pending": len(self.pending),
                "active": self.active is not None, "last_used": self.last_used}


class QueuedRequest:
    def __init__(self, request: ProviderRequest, cancel: threading.Event | None):
        self.request = request
        self.cancel = cancel
        self.done = threading.Event()
        self.result: ProviderResult | None = None

    def finish(self, result: ProviderResult) -> None:
        if not self.done.is_set():
            self.result = result
            self.done.set()


@dataclass
class InFlight:
    """A request currently executing on behalf of possibly many coalesced callers."""

    identity: str
    group: str
    event: threading.Event = field(default_factory=threading.Event)
    result: EvidenceBundle | None = None
    waiters: int = 0
    session_key: str = ""
    waiter_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class TargetRecord:
    module: str
    artifact: Path
    digest: str
    profile: dict  # admitted profile identity
    target_profile: object  # the original TargetProfile (or dict), for the provider
    mapping: dict
    provider: str
    engine: str


class MetricsLedger:
    """P8.7 measurement: per-escalation records plus per-correlation-group new-evidence accounting, so
    two wrappers over one engine never count their shared evidence twice."""

    def __init__(self):
        self.records: list[EscalationMetrics] = []
        self.new_groups: set[str] = set()
        self._lock = threading.Lock()

    def record(self, m: EscalationMetrics) -> None:
        with self._lock:
            self.records.append(m)

    def mark_new_evidence(self, group: str) -> bool:
        with self._lock:
            if group in self.new_groups:
                return False
            self.new_groups.add(group)
            return True

    def summary(self) -> dict:
        with self._lock:
            records = list(self.records)
        return {"records": len(records),
                "cold_starts": sum(1 for r in records if r.cold_start),
                "warm_runs": sum(1 for r in records if not r.cold_start),
                "evidence_reused": sum(1 for r in records if r.evidence_reused),
                "evidence_new": sum(1 for r in records if r.evidence_new),
                "new_evidence_groups": len(self.new_groups),
                "verified_improvements": sum(1 for r in records if r.verified_improvement),
                "resolved_unknowns": sum(1 for r in records if r.resolved_unknown),
                "total_wall_seconds": sum(r.wall_seconds for r in records),
                "total_worker_seconds": sum(r.worker_seconds for r in records),
                "compiles_before": sum(r.compiles_before for r in records),
                "compiles_after": sum(r.compiles_after for r in records)}


class Broker:
    """The narrow analysis contract (P8.1): capabilities, function/neighbourhood evidence, focused
    investigations, refresh, idle-session reaping and measurement. Callers register targets first; every
    request is admitted, provider-pinned, coalesced, budgeted and attributed."""

    def __init__(self, *, cache_dir: Path | str | None = None, policy: EscalationPolicy | None = None,
                 max_sessions: int = 4, idle_seconds: float = 1800.0, max_pending_per_session: int = 32):
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.policy = policy or EscalationPolicy()
        self.max_sessions = max(1, max_sessions)
        self.idle_seconds = idle_seconds
        self.max_pending_per_session = max(1, max_pending_per_session)
        self._providers: dict[str, Provider] = {}
        self._targets: dict[str, TargetRecord] = {}
        self._sessions: dict[str, Session] = {}
        self._inflight: dict[str, InFlight] = {}
        self._portfolios: dict[str, CandidatePortfolio] = {}
        self._metrics = MetricsLedger()
        self._lock = threading.RLock()
        self._denials: list[dict] = []

    # ------------------------------------------------------------ registration

    def register_provider(self, provider: Provider) -> None:
        with self._lock:
            self._providers[provider.name] = provider

    def register_target(self, module: str, artifact: Path | str, profile, *, mapping: dict | None = None,
                        provider: str | None = None) -> TargetRecord:
        """Admit a target once: resolve format/host/arch/mode against the configured capability manifest,
        pin the provider and record the admitted profile identity (P8.1/P8.2)."""
        artifact = Path(artifact)
        data = artifact.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        chosen: Provider | None = None
        admission: CapabilityResult | None = None
        if provider:
            chosen = self._providers.get(provider)
            if chosen is None:
                raise CapabilityError(f"provider {provider!r} is not registered; available: "
                                      f"{', '.join(sorted(self._providers))}")
            admission = chosen.capabilities(profile, artifact)
        else:
            for name in sorted(self._providers):
                admission = self._providers[name].capabilities(profile, artifact)
                if admission.supported:
                    chosen = self._providers[name]
                    break
        if chosen is None or admission is None or not admission.supported:
            reasons = tuple(admission.reasons) if admission else ()
            raise CapabilityError(f"target {module!r} is not admitted by any configured provider: "
                                  f"{'; '.join(reasons) or 'no provider registered'}")
        record = TargetRecord(module=module, artifact=artifact, digest=digest,
                              profile=dict(admission.profile or {}),
                              target_profile=profile if isinstance(profile, dict) else profile,
                              mapping=dict(mapping or {}), provider=chosen.name,
                              engine=chosen.engine)
        with self._lock:
            self._targets[module] = record
        return record

    def capabilities(self, target_profile, artifact=None, *, provider: str | None = None) -> CapabilityResult:
        """Admission against a chosen provider's manifest without starting anything."""
        if provider:
            prov = self._providers.get(provider)
            if prov is None:
                raise CapabilityError(f"provider {provider!r} is not registered")
            return prov.capabilities(target_profile, artifact)
        results = []
        for name in sorted(self._providers):
            r = self._providers[name].capabilities(target_profile, artifact)
            results.append(r)
            if r.supported:
                return r
        reasons: tuple = ()
        for r in results:
            reasons = (*reasons, *r.reasons)
        return CapabilityResult(False, "", reasons or ("no provider registered",))

    # ------------------------------------------------------------ lookup

    def _target(self, function_key: str) -> TargetRecord:
        module = function_key.split("::", 1)[0]
        record = self._targets.get(module)
        if record is None:
            raise TargetError(f"function {function_key!r}: module {module!r} was never admitted through "
                              f"register_target")
        return record

    def _pin_provider(self, target: TargetRecord, provider: str | None) -> Provider:
        if provider:
            prov = self._providers.get(provider)
            if prov is None:
                raise CapabilityError(f"provider {provider!r} is not registered")
            return prov
        return self._providers[target.provider]  # pinned at admission; failures are returned, not switched

    def _session_key(self, target: TargetRecord, provider: str | None = None) -> str:
        """Session key derived from artifact, module, mapping, analysis profile and owning provider
        (P8.3): a direct import and a REA sidecar are different owned resources even over one target."""
        payload = {"artifact": target.digest, "module": target.module, "mapping": target.mapping,
                   "profile": target.profile, "provider": provider or target.provider}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        return f"{target.module}:{digest[:20]}"

    def session_states(self) -> dict:
        with self._lock:
            return {key: s.describe() for key, s in self._sessions.items()}

    def inflight_requests(self) -> dict:
        """Diagnostics: the currently executing shared requests (identity -> waiters/group/event)."""
        with self._lock:
            return {ident: {"group": inf.group, "waiters": inf.waiters, "waiter_event": inf.waiter_event}
                    for ident, inf in self._inflight.items()}

    def portfolio(self, function_key: str) -> CandidatePortfolio:
        with self._lock:
            return self._portfolios.setdefault(function_key, CandidatePortfolio(function_key))

    # ------------------------------------------------------------ the contract

    def get_function_evidence(self, function_key: str, facets, snapshot: str = "current",
                              budget: Budget | None = None, *, provider: str | None = None,
                              requester: str = "", cancel: threading.Event | None = None,
                              limits: dict | None = None) -> EvidenceBundle:
        target = self._target(function_key)
        prov = self._pin_provider(target, provider)
        name = function_key.split("::", 1)[1] if "::" in function_key else function_key
        return self._evidence_request(target, prov, (name,), facets, snapshot, budget, requester, cancel,
                                      limits or {}, kind="function")

    def get_neighbourhood_evidence(self, function_key: str, limits: dict | None = None,
                                   snapshot: str = "current", budget: Budget | None = None, *,
                                   provider: str | None = None, requester: str = "",
                                   cancel: threading.Event | None = None) -> EvidenceBundle:
        """Evidence for the function and its small neighbourhood (direct callees within the module),
        bounded by `limits` (max_functions, max_depth=1 by default) — one shared import, bounded views."""
        limits = {**{"max_functions": 8, "max_depth": 1}, **(limits or {})}
        target = self._target(function_key)
        prov = self._pin_provider(target, provider)
        module = function_key.split("::", 1)[0]
        primary_name = function_key.split("::", 1)[1] if "::" in function_key else function_key
        primary = self._evidence_request(target, prov, (primary_name,), ("calls", "instructions"), snapshot,
                                         budget, requester, cancel, limits, kind="neighbourhood-primary")
        if not primary.ok():
            return primary
        names = {primary_name}
        for evidence in primary.functions.values():
            for call in (evidence.views.get("calls") or {}).get("items", []):
                if call.get("indirect") or not call.get("name"):
                    continue
                if len(names) - 1 >= limits["max_functions"]:
                    break
                names.add(str(call["name"]))
        if len(names) == 1:
            return primary
        return self._evidence_request(target, prov, tuple(sorted(names)), ("calls", "instructions"), snapshot,
                                      budget, requester, cancel, limits, kind="neighbourhood")

    def _evidence_request(self, target: TargetRecord, prov: Provider, names: tuple, facets,
                          snapshot: str, budget: Budget | None, requester: str,
                          cancel: threading.Event | None, limits: dict, kind: str) -> EvidenceBundle:
        facets = tuple(sorted(set(facets) | {"pseudocode"}))
        budget = (budget or Budget()).clone()
        identity = _identity(prov.engine, target.profile, target.digest, facets, names, kind)
        group = _correlation_group(prov.engine, target.profile, target.digest)
        request = ProviderRequest(request_id=str(uuid.uuid4()), kind=kind,
                                  function_key=f"{target.module}::{names[0]}", functions=names,
                                  facets=facets, snapshot=snapshot, budget=budget, limits=dict(limits),
                                  profile=target.profile,
                                  inputs={"artifact": str(target.artifact), "module": target.module,
                                          "profile": target.target_profile, "mapping": target.mapping},
                                  requester=requester)
        with self._lock:
            existing = self._inflight.get(identity)
            if existing is None:
                existing = InFlight(identity=identity, group=group)
                self._inflight[identity] = existing
                claimed = True
            else:
                existing.waiters += 1
                existing.waiter_event.set()  # observable for diagnostics/tests, never blocks
                claimed = False
        if claimed:
            try:
                result, session_key = self._execute(prov, target, request, cancel)
                existing.session_key = session_key
                bundle = self._bundle(result, request, prov, group, requester, snapshot)
            except Exception as e:  # the owner never strands waiters
                bundle = EvidenceBundle(request_id=request.request_id, requester=requester,
                                        function_keys=request.functions, snapshot=snapshot,
                                        provider=prov.name, engine=prov.engine,
                                        correlation_group=group, status="failed",
                                        diagnostics=[f"broker failure: {e}"], budget=budget)
            finally:
                with self._lock:
                    existing.result = bundle
                    existing.event.set()
                    self._inflight.pop(identity, None)
            return bundle
        return self._wait_shared(existing, requester, cancel, budget)

    def _wait_shared(self, inflight: InFlight, requester: str, cancel: threading.Event | None,
                     budget: Budget) -> EvidenceBundle:
        while not inflight.event.is_set():
            if cancel is not None and cancel.is_set():
                with self._lock:
                    inflight.waiters -= 1
                return EvidenceBundle(request_id=str(uuid.uuid4()), requester=requester,
                                      function_keys=(), snapshot="current", provider="", engine="",
                                      correlation_group=inflight.group, status="cancelled",
                                      diagnostics=["waiter cancelled: the shared request continues for its "
                                                   "other consumers"], budget=budget)
            inflight.event.wait(timeout=0.1)
        with self._lock:
            inflight.waiters -= 1
        result = inflight.result
        if result is None:
            return EvidenceBundle(request_id=str(uuid.uuid4()), requester=requester, function_keys=(),
                                  snapshot="current", provider="", engine="",
                                  correlation_group=inflight.group, status="retryable",
                                  diagnostics=["the shared request ended without a result: retry"], budget=budget)
        if result.status == "cancelled" and result.requester != requester:
            return EvidenceBundle(request_id=str(uuid.uuid4()), requester=requester,
                                  function_keys=result.function_keys, snapshot=result.snapshot,
                                  provider=result.provider, engine=result.engine,
                                  correlation_group=result.correlation_group, status="retryable",
                                  diagnostics=["the shared request was cancelled by its owner and the session "
                                               "was invalidated: retry explicitly"], budget=result.budget,
                                  shared=True)
        return EvidenceBundle(request_id=str(uuid.uuid4()), requester=requester,
                              function_keys=result.function_keys, snapshot=result.snapshot,
                              provider=result.provider, engine=result.engine,
                              correlation_group=result.correlation_group, status=result.status,
                              functions=result.functions, refs=result.refs, diagnostics=list(result.diagnostics),
                              reused=result.reused, from_cache=result.from_cache,
                              wall_seconds=result.wall_seconds, worker_seconds=result.worker_seconds,
                              budget=result.budget, shared=True)

    # ------------------------------------------------------------ session execution

    def _execute(self, prov: Provider, target: TargetRecord, request: ProviderRequest,
                 cancel: threading.Event | None) -> tuple[ProviderResult, str]:
        session_key = self._session_key(target, prov.name)
        with self._lock:
            session = self._sessions.get(session_key)
            if session is not None and session.state in ("failed", "cancelled"):
                self._sessions.pop(session_key, None)  # re-establish explicitly on the next request
                session = None
            if session is not None and session.state == "draining":
                reason = f"session {session_key} is draining: request denied, retry later"
                self._denials.append({"session": session_key, "request": request.request_id, "reason": reason})
                return ProviderResult(status="denied", diagnostics=[reason], denied_reason=reason), session_key
            if session is None:
                if len(self._sessions) >= self.max_sessions:
                    self._evict_lru_locked(f"session quota {self.max_sessions} reached")
                session = Session(session_key, prov, target.profile)
                self._sessions[session_key] = session
            session.touch()
        result = self._run_session(session, request, cancel)
        return result, session_key

    def _run_session(self, session: Session, request: ProviderRequest,
                     cancel: threading.Event | None) -> ProviderResult:
        q = QueuedRequest(request, cancel)
        with self._lock:
            if len(session.pending) >= self.max_pending_per_session:
                reason = f"session queue full ({self.max_pending_per_session} pending): request denied"
                self._denials.append({"session": session.key, "request": request.request_id, "reason": reason})
                return ProviderResult(status="denied", diagnostics=[reason], denied_reason=reason)
            session.pending.append(q)
        self._pump(session)
        while not q.done.is_set():
            if cancel is not None and cancel.is_set():
                with self._lock:
                    if session.active is q:
                        session.cancel("the active shared request was cancelled: the session is invalidated")
                        self._finish_pending(session, skip=q, status="retryable",
                                             why="session cancelled with the active request")
                        q.finish(ProviderResult(status="cancelled",
                                                diagnostics=["the active request was cancelled; the session "
                                                             "was invalidated"]))
                    else:
                        try:
                            session.pending.remove(q)
                        except ValueError:
                            pass
                        q.finish(ProviderResult(status="cancelled",
                                                diagnostics=["the waiting request was cancelled; shared work "
                                                             "continues for other consumers"]))
                break
            q.done.wait(timeout=0.1)
        return q.result or ProviderResult(status="retryable", diagnostics=["request dropped"])

    def _pump(self, session: Session) -> None:
        """The serial request stream: exactly one request executes per session; the finishing caller
        pumps the next, preserving FIFO order."""
        with session.serial:
            while True:
                with self._lock:
                    if session.active is not None:
                        return
                    if not session.pending:
                        return
                    if session.state == "closed" and not session.start():
                        self._finish_pending(session, status="retryable",
                                             why=f"session failed to start: {session.failure}")
                        return
                    if session.state != "ready":
                        self._finish_pending(session, status="retryable",
                                             why=f"session {session.state}: {session.failure}")
                        return
                    q = session.pending.popleft()
                    session.active = q
                result = self._fetch(session, q)
                with self._lock:
                    session.active = None
                    q.finish(result)
                    if session.state == "cancelled":
                        self._finish_pending(session, skip=q, status="retryable",
                                             why="the session was cancelled")
                        return
                    if q.cancel is not None and q.cancel.is_set():
                        # cancelling the active provider operation invalidates the whole session
                        session.cancel("the active shared request was cancelled: the session is invalidated")
                        self._finish_pending(session, skip=q, status="retryable",
                                             why="session cancelled with the active request")
                        return

    def _fetch(self, session: Session, q: QueuedRequest) -> ProviderResult:
        start = time.monotonic()
        try:
            result = session.provider.fetch(session.key, session.handle, q.request, q.cancel)
            result.wall_seconds = max(result.wall_seconds, time.monotonic() - start)
            return result
        except ProviderError as e:
            with self._lock:
                session.cancel(f"provider failure: {e}")
                self._finish_pending(session, skip=q, status="retryable",
                                     why=f"the shared session failed ({e}): retry explicitly")
            return ProviderResult(status="retryable", diagnostics=[f"session failed: {e}"])

    def _finish_pending(self, session: Session, *, skip: QueuedRequest | None = None, status: str,
                        why: str) -> None:
        for queued in list(session.pending):
            if queued is skip:
                continue
            queued.finish(ProviderResult(status=status, diagnostics=[why]))

    def _bundle(self, result: ProviderResult, request: ProviderRequest, prov: Provider, group: str,
                requester: str, snapshot: str) -> EvidenceBundle:
        status = result.status
        if status == "timeout":
            status = "budget_exceeded"
        if status not in BUNDLE_STATUSES:
            status = "failed"
        if not result.reused and result.status in ("ok", "partial"):
            self._metrics.mark_new_evidence(group)
        return EvidenceBundle(request_id=request.request_id, requester=requester,
                              function_keys=request.functions, snapshot=snapshot, provider=prov.name,
                              engine=prov.engine, correlation_group=group, status=status,
                              functions=dict(result.functions), refs=dict(result.refs),
                              diagnostics=list(result.diagnostics), reused=result.reused,
                              wall_seconds=result.wall_seconds, worker_seconds=result.worker_seconds,
                              budget=request.budget)

    # ------------------------------------------------------------ investigations (P8.5/P8.6)

    def propose_investigation(self, function_key: str, question: str, evidence_refs=None,
                              budget: Budget | None = None, *, snapshot: str = "current",
                              requester: str = "", cancel=None):
        """A focused investigation through the broker: coalesced evidence, ranked hypotheses, minimal
        candidate changes and a stopping condition. Candidates attach to the snapshot that produced
        them; applying one under a different snapshot is refused (A-06)."""
        start = time.monotonic()
        investigation = run_investigation(self, function_key, question, evidence_refs, budget,
                                          snapshot=snapshot, requester=requester, cancel=cancel)
        wall = max(0.0, time.monotonic() - start)
        self._metrics.record(EscalationMetrics(escalation_id=investigation.investigation_id,
                                               function_key=function_key, action="investigate_function",
                                               cold_start=False, wall_seconds=wall,
                                               worker_seconds=investigation.budget_consumed.worker_seconds,
                                               outcome=investigation.status))
        self.policy.observe("investigate_function", improved=False, cost=wall)
        return investigation

    # ------------------------------------------------------------ refresh / flush / eviction

    def refresh_evidence(self, scope: str, new_snapshot: str) -> int:
        """A context change: invalidate cached evidence for the scope, demote old-snapshot candidates to
        historical (retained) and re-resolve on demand (P8.6/A-06)."""
        module = scope.split("::", 1)[0]
        with self._lock:
            sessions = list(self._sessions.items())
            for key in list(self._portfolios):
                if scope == "*" or key == scope or key.startswith(module):
                    self._portfolios[key].on_snapshot_change(new_snapshot)
        invalidated = 0
        for session_key, session in sessions:
            if scope != "*" and not session_key.startswith(module):
                continue
            try:
                invalidated += session.provider.refresh(session_key, scope, new_snapshot)
            except ProviderError:
                pass
        return invalidated

    def flush_metadata(self, function_key: str, *, view_invalidated: bool = False, reason: str = "") -> None:
        """flushCache() on metadata changes; the stronger reset/reimport when the Program view is
        invalidated (P8.3/P7.5)."""
        target = self._target(function_key)
        session_key = self._session_key(target)
        with self._lock:
            session = self._sessions.get(session_key)
        if session is not None:
            session.provider.flush(session_key, view_invalidated=view_invalidated, reason=reason)
            if view_invalidated:
                with self._lock:
                    session.cancel(f"view invalidated: {reason}")
                    self._finish_pending(session, status="retryable",
                                         why="the Program view was invalidated: retry explicitly")

    def close_idle_sessions(self) -> list[dict]:
        """Idle eviction with recorded reasons (P8.3)."""
        now = time.monotonic()
        closed = []
        with self._lock:
            for key, session in list(self._sessions.items()):
                if session.active is None and now - session.last_used >= self.idle_seconds:
                    reason = f"idle for {now - session.last_used:.0f}s (limit {self.idle_seconds:.0f}s)"
                    session.close()
                    closed.append({"session": key, "reason": reason})
                    self._sessions.pop(key, None)
        return closed

    def _evict_lru_locked(self, reason: str) -> None:
        if not self._sessions:
            return
        victim = min(self._sessions.values(), key=lambda s: s.last_used)
        self._denials.append({"session": victim.key, "reason": f"evicted: {reason}"})
        victim.close()
        self._sessions.pop(victim.key, None)

    # ------------------------------------------------------------ measurement (P8.7)

    def record_escalation(self, *, action: str, function_key: str, cold_start: bool = False,
                          wall_seconds: float = 0.0, worker_seconds: float = 0.0,
                          evidence_reused: bool = False, evidence_new: bool = False,
                          additional_tokens: int = 0, compiles_before: int = 0, compiles_after: int = 0,
                          verified_improvement: bool = False, resolved_unknown: bool = False,
                          outcome: str = "") -> EscalationMetrics:
        m = EscalationMetrics(escalation_id=str(uuid.uuid4()), function_key=function_key, action=action,
                              cold_start=cold_start, wall_seconds=wall_seconds, worker_seconds=worker_seconds,
                              evidence_reused=evidence_reused, evidence_new=evidence_new,
                              additional_tokens=additional_tokens, compiles_before=compiles_before,
                              compiles_after=compiles_after, verified_improvement=verified_improvement,
                              resolved_unknown=resolved_unknown, outcome=outcome)
        self._metrics.record(m)
        self.policy.observe(action, improved=verified_improvement, cost=wall_seconds)
        return m

    def metrics_summary(self) -> dict:
        return self._metrics.summary()

    def denials(self) -> list[dict]:
        with self._lock:
            return list(self._denials)
