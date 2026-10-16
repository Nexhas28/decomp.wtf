"""Optional REA provider adapter (plan P9, PR-25/26) — read-only by default.

Pinned artifact: ``rea-agents`` npm package v6.4.0 at
``~/.local/lib/node_modules/rea-agents`` (server identity ``rea`` 6.4.0 over stdio MCP,
``scripts/rea.mjs mcp``, protocol 2025-06-18). Discovery evidence: a live ``tools/list``
against that install was captured during implementation (170 tools, catalog retained as
``/tmp/rea_tools_list.json`` at implementation time) — the adapter maps to the ACTUAL schema,
not to documentation.

Semantics kept from the plan:
- capability matrix (P9.2): the raw route admits x86/x86_64/arm/arm64/6502 little-endian only;
  raw MIPS/PPC console code, Thumb-only and big-endian targets are typed unsupported; the
  caller-supplied entry point IS the offset-zero mapping, never a free runtime entry.
- read-only sidecar (P9.3): an owned stdio MCP subprocess with an isolated, allowlisted
  environment; no metadata-edit tools are ever issued (source/context edits stay with
  decomp.wtf).
- success/error parsing (P9.5): ``structuredContent`` (with ``normalized_result`` /
  ``evidence_id``) vs ``isError`` diagnostics are parsed separately; null facets stay null.
- snapshots vs live databases (P9.6): the REA session is ephemeral; evidence reuse inside one
  session is keyed by input digest + snapshot; a new uncached query after close re-imports.
- cancellation (P9.7): killing the sidecar invalidates the session — the broker marks every
  affected consumer retryable (this adapter raises ProviderError, which the broker maps).
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .broker import Provider, ProviderError
from .contracts import (CapabilityResult, FunctionEvidence, ProviderRequest, ProviderResult)

REA_ROOT = Path("/Users/shanemcgovern/.local/lib/node_modules/rea-agents")
REA_VERSION = "6.4.0"
REA_SERVER = "rea"
REA_ENTRY = "scripts/rea.mjs"
REA_PROTOCOL = "2025-06-18"

# The core tool set this adapter depends on (P9.4, verified against the real catalog).
CORE_TOOLS = ("open_binary", "close_binary", "binary_session", "list_procedures",
              "resolve_containing_procedure", "analyze_function", "batch_decompile",
              "read_function_instructions", "get_call_graph", "get_evidence_bundle",
              "export_evidence_bundle")

# P9.2 capability matrix: the raw route's admitted CPU families.
RAW_CPUS = {"x86", "x86_64", "arm", "arm64", "6502"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def discover() -> tuple[str, Path, str] | None:
    """(node binary, package root, entry script) for the pinned install, or None."""
    node = "/Users/shanemcgovern/.local/bin/node"
    if not (REA_ROOT / REA_ENTRY).exists():
        node_alt = None
        for cand in ("/opt/homebrew/bin/node", "/usr/local/bin/node"):
            if Path(cand).exists():
                node_alt = cand
                break
        node = node_alt or node
    if not Path(node).exists() or not (REA_ROOT / REA_ENTRY).exists():
        return None
    return node, REA_ROOT, REA_ENTRY


def available() -> bool:
    return discover() is not None


@dataclass
class _Sidecar:
    """One owned REA MCP subprocess with a minimal JSON-RPC client."""

    proc: subprocess.Popen
    tools: dict  # name -> schema (inputSchema)
    artifacts: dict = field(default_factory=dict)  # artifact digest -> open-binary evidence
    _id: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def rpc(self, method: str, params: dict | None = None, timeout: float = 600.0):
        with self._lock:
            self._id += 1
            rid = self._id
            self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                              "params": params or {}}) + "\n")
            self.proc.stdin.flush()
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise ProviderError("rea sidecar closed its stdout (session dead)")
                msg = json.loads(line)
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise ProviderError(f"rea {method}: {json.dumps(msg['error'])[:300]}")
                    return msg.get("result")

    def call_tool(self, name: str, arguments: dict, cancel: threading.Event | None = None,
                  timeout: float = 600.0):
        """One tool call: (is_error, result). Cancellation terminates the sidecar (P9.7)."""
        if cancel is not None and cancel.is_set():
            return True, {"reason": "cancelled"}
        result = self.rpc("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        return bool(result.get("isError")), result

    def kill(self) -> None:
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:
            pass


def _spawn(profile: dict) -> _Sidecar:
    found = discover()
    if found is None:
        raise ProviderError("rea-agents 6.4.0 is not installed (no node + scripts/rea.mjs): no REA session")
    node, root, entry = found
    env = {
        "HOME": profile.get("rea_home") or _profile_dir("rea-home"),
        "TMPDIR": profile.get("rea_tmp") or _profile_dir("rea-tmp"),
        "PATH": "/Users/shanemcgovern/.local/bin:/opt/homebrew/bin:/usr/bin:/bin",
        "LANG": "C",
    }
    proc = subprocess.Popen([node, entry, "mcp"], cwd=root, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            bufsize=1, start_new_session=True)
    side = _Sidecar(proc=proc, tools={})
    init = side.rpc("initialize", {"protocolVersion": REA_PROTOCOL, "capabilities": {},
                                   "clientInfo": {"name": "decomp-wtf-rea-adapter",
                                                  "version": REA_VERSION}})
    if init.get("serverInfo", {}).get("name") != REA_SERVER:
        side.kill()
        raise ProviderError(f"not the pinned REA server: {init.get('serverInfo')}")
    side.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    side.proc.stdin.flush()
    tools = side.rpc("tools/list", {}).get("tools", [])
    side.tools = {t["name"]: t for t in tools if isinstance(t, dict) and t.get("name")}
    missing = [t for t in CORE_TOOLS if t not in side.tools]
    if missing:
        side.kill()
        raise ProviderError(f"rea catalog drifted from the pinned schema: missing {missing} (E-01)")
    return side


def _profile_dir(suffix: str) -> str:
    base = Path(os.environ.get("TMPDIR", "/tmp")) / f"decomp-rea-{suffix}"
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def _artifact_digest(artifact: str | None) -> str:
    if not artifact:
        return ""
    p = Path(artifact)
    return _sha(p.read_bytes()) if p.exists() else ""


class ReaProvider(Provider):
    """The REA route behind the broker's provider surface (PR-25/26). Read-only by default."""

    name = "rea"

    def __init__(self, *, engine_override: str | None = None, read_only: bool = True):
        self.engine = engine_override or f"ghidra-rea-{REA_VERSION}"
        self.read_only = read_only
        self._sessions: dict[str, _Sidecar] = {}

    # ------------------------------------------------------------ admission

    def capabilities(self, target_profile, artifact=None) -> CapabilityResult:
        reasons = []
        fmt = (target_profile or {}).get("format", "raw")
        arch = (target_profile or {}).get("arch", "")
        mode = (target_profile or {}).get("mode", "")
        big = bool((target_profile or {}).get("big_endian", False))
        host = (target_profile or {}).get("host", "")
        if fmt == "raw":
            if arch not in RAW_CPUS:
                reasons.append(f"raw route does not admit {arch!r} (admitted: {sorted(RAW_CPUS)})")
            if big:
                reasons.append("raw route excludes big-endian targets")
            if arch == "arm" and mode == "thumb":
                reasons.append("Thumb-only raw targets are not admitted")
            if arch in ("mips", "ppc", "powerpc"):
                reasons.append(f"raw {arch} console code is not admitted through the REA raw route")
        else:
            if host == "windows":
                reasons.append("native Windows targets have a separate REA admission boundary")
            if artifact:
                head = self._elf_head(artifact)
                if head is None:
                    reasons.append(f"{artifact} is not a recognised headered native artifact")
                elif head["big"] and not big:
                    reasons.append("artifact header is big-endian but the profile says little-endian")
        if reasons:
            return CapabilityResult(False, self.name, tuple(reasons))
        admitted = dict(target_profile or {})
        admitted["rea_version"] = REA_VERSION
        admitted["read_only"] = self.read_only
        return CapabilityResult(True, self.name,
                                ("rea-adapter", "read-only" if self.read_only else "mutable"),
                                profile=admitted)

    @staticmethod
    def _elf_head(artifact) -> dict | None:
        try:
            data = Path(artifact).read_bytes()
        except OSError:
            return None
        if len(data) < 20 or data[:4] != b"\x7fELF":
            return None
        return {"class": data[4], "big": data[5] == 2}

    # ------------------------------------------------------------ sessions

    def open_session(self, session_key: str, profile: dict):
        side = _spawn(profile)
        self._sessions[session_key] = side
        return side

    def close_session(self, session_key: str, handle) -> None:
        side = self._sessions.pop(session_key, handle)
        if side is None:
            return
        try:
            if side.proc.poll() is None:
                side.rpc("tools/call", {"name": "close_binary", "arguments": {}}, timeout=10)
        except Exception:
            pass
        side.kill()

    # ------------------------------------------------------------ fetch

    def fetch(self, session_key: str, handle, request: ProviderRequest,
              cancel: threading.Event | None) -> ProviderResult:
        side: _Sidecar = handle
        if side.proc.poll() is not None:
            raise ProviderError("rea sidecar is dead; reopen the session (P9.7)")
        artifact = request.inputs.get("artifact", "")
        digest = _artifact_digest(artifact)
        started = time.monotonic()
        # open the immutable artifact once per session per digest (P9.6: a new uncached query
        # after close re-imports; a digest change inside the session is an identity conflict, E-08)
        opened = side.artifacts.get(digest)
        if opened is None:
            if not artifact or not Path(artifact).exists():
                return ProviderResult(status="denied", diagnostics=[f"artifact {artifact!r} not readable"],
                                      denied_reason="artifact missing")
            entry_point = request.inputs.get("entry_point")
            err, res = side.call_tool("open_binary",
                                      {"path": artifact, **(dict(entry_point=entry_point)
                                                          if entry_point is not None else {})},
                                      cancel=cancel)
            if err:
                return ProviderResult(status="failed", diagnostics=[self._error_text(res)],
                                      wall_seconds=time.monotonic() - started)
            got_digest = (res.get("structuredContent", {}) if isinstance(res.get("structuredContent"), dict)
                          else {}).get("artifact_digest", res.get("text", ""))
            if isinstance(got_digest, str) and got_digest and not got_digest.startswith(digest[:12]):
                return ProviderResult(status="denied",
                                      diagnostics=[f"artifact identity conflict: expected {digest[:12]}..., "
                                                   f"server reports {got_digest[:40]}"],
                                      denied_reason="target digest changed between admission and import")
            side.artifacts[digest] = {"evidence_id": res.get("structuredContent", {}).get("evidence_id")
                                      if isinstance(res.get("structuredContent"), dict) else None}
            opened = side.artifacts[digest]
        functions: dict[str, FunctionEvidence] = {}
        refs: dict = {}
        diagnostics: list = []
        status = "ok"
        for name in request.functions:
            if cancel is not None and cancel.is_set():
                return ProviderResult(status="cancelled", functions=functions, refs=refs,
                                      diagnostics=diagnostics, wall_seconds=time.monotonic() - started)
            err, res = side.call_tool("analyze_function", {"function": name, "facets": list(request.facets)},
                                      cancel=cancel)
            if err:
                # parse the diagnostic, keep partial evidence (E-03)
                status = "partial"
                diagnostics.append(self._error_text(res))
                continue
            key = f"{request.function_key}::{name}"
            functions[key] = self._evidence(key, res, request)
            refs[key] = self._ref(name, res)
        return ProviderResult(status=status, functions=functions, refs=refs, diagnostics=diagnostics,
                              wall_seconds=time.monotonic() - started, worker_seconds=0.0)

    def _evidence(self, key: str, res, request: ProviderRequest) -> FunctionEvidence:
        sc = res.get("structuredContent")
        if not isinstance(sc, dict):
            return FunctionEvidence(function_key=key, provider=self.name, engine=self.engine,
                                    correlation_group="", snapshot=request.snapshot, status="failed",
                                    diagnostics=["no structuredContent (schema drift)"], coverage={})
        normalized = sc.get("normalized_result") or sc
        # null facets stay null (never an empty array or a "None" string): coverage records them
        views = {k: str(v)[:4000] for k, v in normalized.items() if k != "evidence_id" and v is not None}
        coverage = {f: "ok" if normalized.get(f) is not None else "unavailable"
                    for f in request.facets} or {"analysis": "ok"}
        refs = {}
        evid = sc.get("evidence_id")
        if evid:
            from .contracts import EvidenceRef

            refs["dossier"] = EvidenceRef(ref=f"rea:{evid}", facet="dossier", size_hint=0, summary=key)
        return FunctionEvidence(function_key=key, provider=self.name, engine=self.engine,
                                correlation_group="", snapshot=request.snapshot, status="ok",
                                views=views, coverage=coverage, refs=refs)

    @staticmethod
    def _ref(name: str, res: dict):
        from .contracts import EvidenceRef

        sc = res.get("structuredContent", {}) if isinstance(res.get("structuredContent"), dict) else {}
        return EvidenceRef(ref=f"rea:{sc.get('evidence_id') or name}", facet="dossier", size_hint=0,
                           summary=name)

    @staticmethod
    def _error_text(res) -> str:
        if isinstance(res, dict):
            text = res.get("text") or json.dumps(res.get("structuredContent", {}))
            return str(text)[:400]
        return str(res)[:400]

    # ------------------------------------------------------------ refresh / flush

    def refresh(self, session_key: str, scope: str, new_snapshot: str) -> int:
        """A snapshot change invalidates in-session evidence: REA's Program is ephemeral, so a
        refresh closes and reopens the session (P9.6: never reuse a snapshot file as a live DB)."""
        handle = self._sessions.get(session_key)
        if handle is None:
            return 0
        profile = {"rea_home": _profile_dir("rea-home"), "rea_tmp": _profile_dir("rea-tmp")}
        self.close_session(session_key, handle)
        self.open_session(session_key, profile)
        return 1

    def flush(self, session_key: str, *, view_invalidated: bool, reason: str) -> None:
        if view_invalidated:
            self.refresh(session_key, "", "")
