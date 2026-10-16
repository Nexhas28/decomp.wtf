"""Durable evidence records (plan 18.5, 18.7): what the broker hands back in memory, persisted.

A record owns its identity and provenance (route, engine and version, analysis-profile digest, correlation
group, snapshot) and points at facet payloads stored content-addressed in the same database. The record
and its payloads are written in one transaction, so a committed record never points at a missing or
half-written payload. The evidence id is a digest of what the evidence *is*, so a coalesced, shared or
re-fetched bundle saves once.
"""

from __future__ import annotations

import hashlib
import json
import time

from .contracts import SCOPE_LIMITATION, EvidenceRecord, FunctionEvidence

# route-only keys (provider wrapper details) never change what was analysed; mirrors broker._ROUTE_KEYS
_ROUTE_KEYS = frozenset({"provider", "provider_route", "route", "rea_version", "read_only",
                         "rea_home", "rea_tmp", "rea-entry", "rea_protocol"})
_FACET_STATUSES = {"ok": "complete", "complete": "complete", "empty": "complete", "partial": "partial",
                   "truncated": "partial", "failed": "failed", "unknown": "unknown"}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def profile_digest(profile) -> str:
    """Digest of the analysis profile with route-only keys stripped (what was analysed, not how it was reached)."""
    if isinstance(profile, dict):
        profile = {k: v for k, v in profile.items() if k not in _ROUTE_KEYS}
    return _digest(profile)


def split_engine(engine: str) -> tuple[str, str]:
    """'ghidra-12.1.4' -> ('ghidra', '12.1.4'); an unversioned engine reports version 'unknown'."""
    name, _, version = (engine or "unknown").partition("-")
    return name or "unknown", version or "unknown"


def record_from_evidence(evidence: FunctionEvidence, *, provider_route: str, engine: str, target_digest: str,
                         profile, correlation_group: str, snapshot: str) -> tuple[EvidenceRecord, dict]:
    """(record, payloads by payload_ref) for one function's evidence. Facet status is mapped conservatively
    from the provider's coverage: a facet with no view is unavailable, never assumed empty."""
    payloads: dict[str, str] = {}
    facets: dict[str, dict] = {}
    unknowns: list[str] = []
    for facet in sorted(set(evidence.views) | set(evidence.coverage)):
        raw = str(evidence.coverage.get(facet, "unknown"))
        view = evidence.views.get(facet)
        if view is None:
            facets[facet] = {"status": "unavailable", "reason": f"not produced by this analysis ({raw})"}
            continue
        body = json.dumps(view, sort_keys=True, default=str)
        ref = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
        payloads[ref] = body
        status = _FACET_STATUSES.get(raw, "unknown")
        if isinstance(view, dict) and view.get("truncated") and status == "complete":
            status = "partial"
        if status == "unknown":
            unknowns.append(f"{facet}: provider coverage not stated")
        facets[facet] = {"status": status, "payload_ref": ref}
    proposals = (evidence.views.get("proposals") or {}).get("items") or []
    limitations = [SCOPE_LIMITATION] + [f"{f}: view truncated to its bounded size" for f, v in evidence.views.items()
                                        if isinstance(v, dict) and v.get("truncated")]
    underlying, version = split_engine(engine)
    pdigest = profile_digest(profile)
    evidence_id = "ev-" + _digest({"function": evidence.function_key, "group": correlation_group,
                                   "profile": pdigest, "target": target_digest, "snapshot": snapshot,
                                   "facets": facets})[:32]
    record = EvidenceRecord(
        evidence_id=evidence_id, function_key=evidence.function_key, target_digest=target_digest,
        provider_route=provider_route, underlying_engine=underlying, engine_version=version,
        analysis_profile_digest=pdigest, correlation_group=correlation_group, snapshot=snapshot,
        status=evidence.status, facets=facets, observations=list(evidence.diagnostics),
        inferences=[dict(p) for p in proposals], unknowns=unknowns, limitations=limitations,
        classification=evidence.classification,
        entry=None if evidence.entry is None else f"0x{int(evidence.entry):08x}")
    return record, payloads


class EvidenceStore:
    """Evidence records in the coordinator's SQLite store (tables evidence_records / evidence_payloads)."""

    def __init__(self, store):
        self.store = store

    def save(self, record: EvidenceRecord, payloads: dict[str, str]) -> bool:
        """Persist a record with its payloads atomically. Returns False when the same evidence was already
        stored (idempotent). Every facet's payload_ref must be supplied or already stored."""
        now = time.time()
        with self.store.tx() as db:
            for ref, body in payloads.items():
                if ref != "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest():
                    raise ValueError(f"payload {ref} does not match its content")
                db.execute("INSERT OR IGNORE INTO evidence_payloads(payload_ref, body, size, created_at) "
                           "VALUES(?,?,?,?)", (ref, body, len(body), now))
            for facet, info in record.facets.items():
                ref = info.get("payload_ref")
                if ref and db.execute("SELECT 1 FROM evidence_payloads WHERE payload_ref=?", (ref,)).fetchone() is None:
                    raise ValueError(f"facet {facet} points at payload {ref}, which was never stored")
            d = record.to_dict()
            cur = db.execute(
                "INSERT OR IGNORE INTO evidence_records(evidence_id, schema_version, function_key, target_digest, "
                "provider_route, underlying_engine, engine_version, analysis_profile_digest, correlation_group, "
                "snapshot, status, facets, observations, inferences, unknowns, limitations, "
                "untrusted_target_content, original_provider_evidence_ref, classification, entry, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d["evidence_id"], d["schema_version"], d["function_key"], d["target_digest"], d["provider_route"],
                 d["underlying_engine"], d["engine_version"], d["analysis_profile_digest"], d["correlation_group"],
                 d["snapshot"], d["status"], *(json.dumps(d[k], sort_keys=True, default=str) for k in (
                     "facets", "observations", "inferences", "unknowns", "limitations")),
                 int(bool(d["untrusted_target_content"])), d["original_provider_evidence_ref"],
                 d["classification"], d["entry"], now))
            return cur.rowcount > 0

    def _record(self, row) -> EvidenceRecord:
        d = dict(row)
        for k in ("facets", "observations", "inferences", "unknowns", "limitations"):
            d[k] = json.loads(d[k])
        d["untrusted_target_content"] = bool(d["untrusted_target_content"])
        return EvidenceRecord.from_dict(d)

    def load(self, evidence_id: str) -> EvidenceRecord | None:
        row = self.store.one("SELECT * FROM evidence_records WHERE evidence_id=?", evidence_id)
        return None if row is None else self._record(row)

    def for_function(self, function_key: str, snapshot: str | None = None) -> list[EvidenceRecord]:
        if snapshot is None:
            rows = self.store.all("SELECT * FROM evidence_records WHERE function_key=? ORDER BY created_at",
                                  function_key)
        else:
            rows = self.store.all("SELECT * FROM evidence_records WHERE function_key=? AND snapshot=? "
                                  "ORDER BY created_at", function_key, snapshot)
        return [self._record(r) for r in rows]

    def payload(self, payload_ref: str):
        row = self.store.one("SELECT body FROM evidence_payloads WHERE payload_ref=?", payload_ref)
        return None if row is None else json.loads(row["body"])
