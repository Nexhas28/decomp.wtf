"""Optional REA provider adapter (plan P9, PR-25/26) — read-only by default (P9.1-P9.8).

The adapter drives a pinned ``rea-agents`` MCP server as a private stdio sidecar and implements
exactly the broker's :class:`~decompnet.analysis.broker.Provider` surface, so the analysis broker
(P8.1-P8.3) can treat REA as one optional evidence route alongside
:class:`~decompnet.analysis.broker.DirectGhidraProvider`. It never mutates the target artifact,
never invents provider operations, and never guesses an analysis profile.

Pinned artifact (P9.1)
----------------------
* Package: ``rea-agents`` **6.4.0** (npm), mcpName ``io.github.morluto/rea``, upstream
  https://github.com/morluto/rea.
* Location on this host: ``~/.local/lib/node_modules/rea-agents`` (the package
  ``bin`` is ``rea`` -> ``scripts/rea.mjs``; MCP mode is ``node scripts/rea.mjs mcp`` which loads
  ``dist/main.js`` and serves the stdio tool catalog).
* Discovery method (2026-10-16): the *installed* package was driven once over stdio with MCP
  ``initialize`` (protocolVersion ``2025-06-18``) + ``tools/list`` + ``shutdown``, with ``HOME``/
  ``TMPDIR`` redirected to throwaway directories (raw catalog captured at
  ``/tmp/rea_tools_list.json``; the same capture is reproducible with
  ``benchmarks/harness/rea_discover.py``). Result: ``serverInfo`` = ``{"name": "rea", "version":
  "6.4.0"}``, **170 tools**. That live catalog — plus the pinned ``dist/`` contracts — is the
  evidence behind every schema fact in this module.
* Tool-name drift versus the plan's reviewed documentation (commit 6183544): the reviewed families
  exist under the names ``open_binary``/``close_binary``, ``binary_session`` (session discovery;
  there is no ``session_status``), ``list_procedures``, ``procedure_address``,
  ``resolve_containing_procedure``, ``procedure_info``/``procedure_pseudo_code``/
  ``procedure_assembly``/``procedure_callees``/``procedure_callers``/``procedure_references``,
  ``read_function_instructions``, ``read_bytes``, ``batch_decompile``, ``get_call_graph``,
  ``trace_native_values``, ``resolve_native_call_targets``, ``inspect_native_data_type``,
  ``analyze_function`` (the function dossier), ``get_evidence_bundle``/``export_evidence_bundle``/
  ``import_evidence_bundle`` and ``inspect_analysis_view`` (retained views).

Wire contract (P9.5; from the pinned ``dist/server/toolResult*.js`` and the live discovery)
--------------------------------------------------------------------------------------------
* Success: ``content[{type:"text", text:<json>}]`` plus ``structuredContent`` (the canonical value).
  Evidence tools carry the envelope ``{evidence_id ("ev_" + 64 lowercase hex), subject, provider,
  analysis_profile, predicate_type, operation, parameters, raw_result, normalized_result,
  confidence, authority, environment, limitations, locations, evidence_links}``. Session/lifecycle
  tools wrap their value as ``structuredContent = {"result": ...}``.
* Error: ``isError: true`` with a JSON diagnostic in the text part. A partial observation survives
  as ``details.partial_observation = {kind:"retained-evidence", evidence_id}``; an
  oversized-but-completed analysis survives as ``recovery.evidence_reference = {kind:
  "retained-evidence", evidence_id}`` with ``response_bytes_at_least`` — the complete evidence is
  retained and reachable through ``inspect_analysis_view`` / ``export_evidence_bundle``; REA never
  truncates an MCP response.
* The Ghidra deep provider (``provider_id: "ghidra"``) imports each target into an *ephemeral*
  database (``import_mode: "ephemeral-source-immutable"``); cancelling an active request terminates
  that database and discards session annotations, and the next provider query imports the original
  artifact again. The adapter therefore invalidates its whole sidecar session on
  cancellation/timeout (P9.7): recovery is close/reopen, never a half-valid session.
* ``open_binary`` identity result: ``{path, format, kind, sha256 (64 lowercase hex), architecture
  (x86|x86_64|arm|arm64|mips|null)}``; a snapshot import must match the binary identity, provider
  and canonical profile exactly (``snapshotMatchesTarget`` in the pinned artifact).

Capability matrix (P9.2)
------------------------
The reviewed raw-binary route (docs/raw-binary.md @6183544: CPU families x86/x86_64/arm/arm64/6502,
little-endian only, no Thumb-only, caller-supplied entry point as the offset-zero mapping) is
**absent from the pinned 6.4.0 MCP schema** (``open_binary`` takes only ``path``/``format``/
``provider_id``/``snapshot_path``). :func:`raw_matrix_admission` still enforces that documented
matrix exactly; because the pinned artifact cannot issue it, every matrix-passing raw profile is
returned as a *typed unsupported* result carrying the gap reason (``rea_raw_route_absent``). The
only headerless lane in the pinned schema is ``format="dos-com"`` (1..65280 bytes, provider-owned
x86:LE:16:Real Mode import: BinaryLoader, load segment 0x1000, entry offset 0x100) and it is
admitted only when the caller explicitly declares it. Headered artifacts are admitted through
header-based detection: little-endian ELF (x86/x86-64/ARM/AArch64/SuperH), the pinned MIPS lane
(ELF32 ET_EXEC MIPS32r2 o32, :func:`mips_lane_admission`), native x86/x86-64 PE (through the
headless Ghidra bridge, with a warning), and Mach-O x86_64/arm64 little-endian. Everything else is a typed
unsupported result — the direct Ghidra adapter remains separately available through the broker.

Facet mapping (P9.4) — only operations present in the pinned catalog are used
-----------------------------------------------------------------------------
* open/close/identity: ``open_binary`` (provider_id="ghidra"), ``binary_session`` (diagnostics).
* canonical function: ``procedure_address``.
* dossier: ``analyze_function`` (pseudocode/assembly/comments/callees/callers/reference edges/
  unresolved calls/referenced strings+names/basic blocks/native API/native value flow).
* instructions: ``read_function_instructions``.
* calls/references: dossier callees/callers/reference edges (named, exact). ``get_call_graph``
  exists but its nodes carry addresses only, so the broker's neighbourhood driver (which consumes
  *named* callees) is served from dossiers; the graph tool is documented, not wired to a facet
  that would lose names (GAP-3).
* bounded value flow / pcode: dossier ``native_value_flow`` (``trace_native_values`` is documented,
  not called speculatively).
* types: **unavailable** on the pinned catalog (no type-inventory tool; ``inspect_native_data_type``
  needs an explicit address and is not a facet inventory) — never guessed (GAP-2).
* retained evidence / oversized results: ``get_evidence_bundle`` (summary/complete),
  ``inspect_analysis_view`` (retained-evidence source), ``export_evidence_bundle``.

Engine correlation (A-04)
-------------------------
``engine`` is ``decompnet.forks.ghidra.provider_version()`` — the *same call* as
``DirectGhidraProvider`` — so both routes report the same underlying engine. If REA evidence
reports a different provider version it is surfaced as a diagnostic, never merged silently.

Gap list a human must approve before enabling the real route
------------------------------------------------------------
* GAP-1: the documented raw-binary route (CPU family + entry_point mapping) is absent from pinned
  6.4.0; every matrix-passing raw profile is typed-unsupported (``rea_raw_route_absent``).
* GAP-2: no type-inventory tool -> the ``types`` facet is always ``unavailable``.
* GAP-3: ``get_call_graph`` nodes carry addresses only -> neighbourhoods use named dossier
  callees; graph traversal is not wired to a facet.
* GAP-4: relocatable objects (ET_REL) are not admitted (the pinned target model has no verified
  object lane).
* GAP-5: big-endian headered ELF outside the pinned MIPS lane is not admitted (no verified lane).
* GAP-6: the adapter pins the package version exactly (``6.4.0``); upgrading REA requires
  re-pinning the schemas here (E-01 fails first, which is the intended behaviour).
* GAP-7: retired (it was the Windows host boundary; decomp.wtf runs on macOS only).
* GAP-8: the broker's correlation payload includes the provider-specific profile dictionary; the
  engine identity is identical across both routes, but exact group *equality* additionally needs
  broker-side normalisation (profile dicts carry route-specific bindings) — a broker follow-up.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import select
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..forks import ghidra
from .broker import Provider
from .contracts import (Budget, CapabilityResult, EvidenceRef, FunctionEvidence, ProviderError,
                        ProviderRequest, ProviderResult)

# ---------------------------------------------------------------- pinned artifact facts (P9.1)

REA_PACKAGE_NAME = "rea-agents"
REA_PACKAGE_VERSION = "6.4.0"
REA_MCP_NAME = "io.github.morluto/rea"
REA_SERVER_NAME = "rea"
REA_PROTOCOL_VERSION = "2025-06-18"
#: Live-discovery evidence (2026-10-16, macOS): MCP initialize + tools/list against the installed
#: package, HOME/TMPDIR redirected; serverInfo {"name": "rea", "version": "6.4.0"}, 170 tools.
REA_DISCOVERY = {
    "method": "live MCP initialize + tools/list over stdio against the installed package",
    "date": "2026-10-16",
    "host": "macOS (darwin)",
    "server": {"name": REA_SERVER_NAME, "version": REA_PACKAGE_VERSION},
    "protocol_version": REA_PROTOCOL_VERSION,
    "tool_count": 170,
    "mcp_name": REA_MCP_NAME,
}
#: Default install location discovered on this host (override with ``command=``/``cwd=`` or $REA_ROOT).
REA_ROOT = Path(os.environ.get("REA_ROOT", Path.home() / ".local/lib/node_modules/rea-agents"))
#: Compatibility aliases used by the analysis package surface.
REA_VERSION = REA_PACKAGE_VERSION
REA_SERVER = REA_SERVER_NAME
REA_ENTRY = "scripts/rea.mjs"
REA_PROTOCOL = REA_PROTOCOL_VERSION

EVIDENCE_ID_RE = re.compile(r"^ev_[a-f0-9]{64}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

#: Ghidra deep-provider id in the pinned artifact (dist/ghidra/GhidraProviderCapabilities.js).
REA_GHIDRA_PROVIDER_ID = "ghidra"
#: open_binary identity architecture enum (dist/domain/binaryTargetTypes.js).
REA_ARCHITECTURES = ("x86", "x86_64", "arm", "arm64", "mips")
REA_FORMATS = ("hopper", "analysis-database", "mach-o", "elf", "pe", "dos-mz", "dos-com",
               "zip", "ipa", "apk", "msix", "appx", "asar", "dmg", "pkg", "plist",
               "javascript", "source-map")
#: Tools whose structuredContent is wrapped as {"result": ...} (lifecycleResultOf).
REA_SESSION_TOOLS = frozenset({
    "open_binary", "close_binary", "binary_session", "get_evidence_bundle",
    "export_evidence_bundle", "import_evidence_bundle", "list_unknowns", "record_unknown",
    "update_unknown", "verify_unknown_resolution", "inspect_address_context",
    "get_navigation_context",
})
#: Exact input-schema constraints pinned from the live tools/list; E-01 validates these.
PINNED_TOOLS: dict[str, dict] = {
    "open_binary": {"required": ["path"],
                    "properties": {"path": {"type": "string"},
                                   "format": {"type": "string", "enum": ["dos-com"]},
                                   "provider_id": {"type": "string", "auto_allowed": True},
                                   "snapshot_path": {"type": "string"}}},
    "close_binary": {"required": [],
                     "properties": {"snapshot_path": {"type": "string"},
                                    "overwrite": {"type": "boolean"}}},
    "binary_session": {"required": []},
    "list_procedures": {"required": []},
    "procedure_address": {"required": ["procedure"]},
    "analyze_function": {"required": ["procedure"]},
    "batch_decompile": {"required": []},
    "procedure_pseudo_code": {"required": ["procedure"]},
    "procedure_assembly": {"required": ["procedure"]},
    "procedure_callees": {"required": ["procedure"]},
    "procedure_callers": {"required": ["procedure"]},
    "procedure_references": {"required": ["procedure"],
                             "properties": {"direction": {"enum": ["incoming", "outgoing"]}}},
    "read_function_instructions": {"required": ["procedure"]},
    "read_bytes": {"required": ["address"]},
    "get_call_graph": {"required": ["address"],
                       "properties": {"direction": {"enum": ["forward", "backward"]}}},
    "trace_native_values": {"required": ["procedure"]},
    "resolve_native_call_targets": {"required": ["address"]},
    "inspect_native_data_type": {"required": []},
    "get_evidence_bundle": {"required": [],
                            "properties": {"detail": {"enum": ["complete", "summary"]}}},
    "export_evidence_bundle": {"required": ["path"]},
    "inspect_analysis_view": {"required": ["source", "view"]},
    "import_evidence_bundle": {"required": ["path"]},
}
#: The pinned tools the adapter maps broker needs onto (P9.4); a curated alias of PINNED_TOOLS.
CORE_TOOLS = tuple(sorted(PINNED_TOOLS))
#: Minimal environment allowlist for the owned sidecar (P9.3): nothing else from the parent
#: environment reaches the REA process. JAVA_HOME/GHIDRA_INSTALL_DIR are injected from the shared
#: workspace discovery (Workstream C) so REA's pinned Ghidra bridge finds the same runtime and install
#: the direct adapter uses.
REA_ENV_ALLOWLIST = ("HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "PATH",
                     "SystemRoot", "PATHEXT", "COMSPEC", "REA_PACKAGE_VERSION",
                     "JAVA_HOME", "GHIDRA_INSTALL_DIR")

#: The documented raw-binary matrix (docs/raw-binary.md @6183544), enforced exactly (P9.2/E-02).
RAW_FAMILIES = {"x86": (16, 32), "x86_64": (64,), "arm": (32,), "arm64": (64,), "6502": (8,)}
#: The pinned MIPS lane id (dist/ghidra/GhidraMipsProfile.js: mips_support_lane).
MIPS_LANE = "elf32-exec-o32-arch32r2-standard-v3"
#: ELF e_machine values admitted through header-based detection on the pinned artifact.
ELF_LE_MACHINES = {3: "x86", 62: "x86_64", 40: "arm", 183: "aarch64", 42: "sh"}
ELF_MIPS_MACHINE = 8
ELF_PPC_MACHINES = (20, 21)
#: PE machine ids admitted (native x86/x86-64 only).
PE_MACHINES = {0x14C: "x86", 0x8664: "x86_64"}
MACHO_CPUTYPES = {0x01000007: "x86_64", 0x0100000C: "arm64"}
PROCESSOR_ALIASES = {"x86": "x86", "i386": "x86", "x86_64": "x86_64", "amd64": "x86_64",
                     "arm": "arm", "aarch64": "arm64", "arm64": "arm64", "sh": "sh",
                     "mips": "mips", "powerpc": "powerpc", "ppc": "powerpc"}

FACETS = ("pseudocode", "instructions", "blocks", "calls", "references", "types", "pcode")
#: Every function dossier carries these keys in the pinned contract; only facets from the request
#: are projected (the rest stay "unknown"), and null/absent stays distinct from empty (E-09).
DOS_COM_MAX_BYTES = 0xFF00


class ReaProviderFailure(ProviderError):
    """A REA sidecar/session failure: the broker fails the session; consumers become retryable."""


class ReaSchemaDrift(ReaProviderFailure):
    """The live MCP catalog differs from the pinned 6.4.0 contract (E-01): fail, never guess."""


class ReaArtifactIdentityError(ReaProviderFailure):
    """Target bytes/digest conflict between admission, import and evidence (E-08)."""


class ReaSessionInvalidError(ReaProviderFailure):
    """The sidecar session was invalidated (cancellation/timeout/view reset): close/reopen."""


def discover() -> tuple[str, Path, str] | None:
    """(node binary, package root, entry script) for the pinned install, or None."""
    node = os.environ.get("NODE", str(Path.home() / ".local/bin/node"))
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


# ---------------------------------------------------------------- target/host matrix (P9.2)

def detect_format(data: bytes) -> str | None:
    """Header-based format detection (the pinned open_binary policy): elf/pe/mach-o/dos-mz."""
    if len(data) >= 4 and data[:4] == b"\x7fELF":
        return "elf"
    if len(data) >= 64 and data[:2] == b"MZ":
        off = int.from_bytes(data[0x3C:0x40], "little")
        if off + 4 <= len(data) and data[off:off + 4] == b"PE\x00\x00":
            return "pe"
        return "dos-mz"
    magics = (b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
              b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca")
    if len(data) >= 4 and data[:4] in magics:
        return "mach-o"
    return None


def elf_header_info(data: bytes) -> dict | None:
    """ELF identification incl. endian-correct e_type/e_machine/e_flags (needed for the MIPS lane)."""
    if len(data) < 52 or data[:4] != b"\x7fELF":
        return None
    cls, enc = data[4], data[5]
    if cls not in (1, 2) or enc not in (1, 2):
        return {"class": cls, "encoding": "unknown", "type": -1, "machine": -1, "flags": 0,
                "invalid": True}
    endian = "little" if enc == 1 else "big"
    flags_off = 48 if cls == 2 else 36  # 64-bit headers grow e_entry/e_phoff/e_shoff to 8 bytes
    return {"class": 32 if cls == 1 else 64, "encoding": endian,
            "type": int.from_bytes(data[16:18], endian),
            "machine": int.from_bytes(data[18:20], endian),
            "flags": int.from_bytes(data[flags_off:flags_off + 4], endian), "invalid": False}


def pe_header_info(data: bytes) -> dict | None:
    """PE machine, width and managed flag (COM descriptor directory) from the headers."""
    if len(data) < 64 or data[:2] != b"MZ":
        return None
    off = int.from_bytes(data[0x3C:0x40], "little")
    if off + 24 > len(data) or data[off:off + 4] != b"PE\x00\x00":
        return None
    machine = int.from_bytes(data[off + 4:off + 6], "little")
    opt = off + 24
    magic = int.from_bytes(data[opt:opt + 2], "little") if opt + 2 <= len(data) else 0
    bits = 64 if magic == 0x20B else 32 if magic == 0x10B else 0
    dd = opt + (112 if magic == 0x20B else 96)  # optional header data directories
    managed = False
    if dd + 16 <= len(data):
        com_rva = int.from_bytes(data[dd + 14 * 8:dd + 14 * 8 + 4], "little")
        com_size = int.from_bytes(data[dd + 14 * 8 + 4:dd + 14 * 8 + 8], "little")
        managed = com_rva != 0 or com_size != 0
    return {"machine": machine, "bits": bits, "managed": managed}


def macho_header_info(data: bytes) -> dict | None:
    """Mach-O magic/endianness and cputype (thin) or the first fat slice's cputype."""
    if len(data) < 8:
        return None
    magic = data[:4]
    fat_be, fat_le, fat64_be, fat64_le = (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
                                           b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca")
    if magic in (fat_be, fat_le):
        endian = "big" if magic == fat_be else "little"
        if len(data) < 16:
            return None
        return {"fat": True, "endian": endian,
                "cputype": int.from_bytes(data[8:12], endian), "thin": False}
    if magic in (fat64_be, fat64_le):
        endian = "big" if magic == fat64_be else "little"
        if len(data) < 24:
            return None
        return {"fat": True, "endian": endian,
                "cputype": int.from_bytes(data[16:20], endian), "thin": False}
    if magic in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf"):
        endian = "big"
    elif magic in (b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"):
        endian = "little"
    else:
        return None
    return {"fat": False, "endian": endian, "cputype": int.from_bytes(data[4:8], endian),
            "thin": True}


def raw_matrix_admission(processor: str, bits: int, endian: str, mode: str,
                         entry_point: int | None) -> tuple[bool, tuple]:
    """The documented raw-binary matrix, enforced exactly (P9.2/E-02).

    x86/x86_64/arm/arm64/6502, little-endian only, no Thumb-only, no MIPS/PPC console code; the
    caller-supplied ``entry_point`` is the address where file offset zero is mapped and is never
    reinterpreted as an arbitrary runtime entry (P9.2/R02).
    """
    reasons = []
    proc = (processor or "").strip().lower()
    if proc not in RAW_FAMILIES:
        reasons.append(f"processor {proc or '?'!r} is not in the documented raw families "
                       f"({', '.join(sorted(RAW_FAMILIES))}); raw MIPS/PPC console code needs the "
                       f"direct adapter or a deliberate upstream extension")
    else:
        allowed = RAW_FAMILIES[proc]
        if bits not in allowed:
            reasons.append(f"raw {proc} requires {allowed}-bit input, not {bits}-bit")
    if (endian or "").lower() != "little":
        reasons.append(f"the reviewed raw route is little-endian only, not "
                       f"{endian or 'unspecified'}-endian")
    if proc == "arm" and (mode or "").lower() != "arm":
        reasons.append(f"Thumb-only input is excluded from the reviewed raw route (mode "
                       f"{mode or 'unspecified'!r}); pin mode='arm' for ARM profiles")
    if not isinstance(entry_point, int) or entry_point < 0:
        reasons.append("the raw route needs the caller-supplied entry_point (the offset-zero "
                       "mapping); it is never reinterpreted as an arbitrary runtime entry")
    return (not reasons), tuple(reasons)


def mips_lane_admission(hdr: dict | None, declared_lane: str | None) -> tuple[bool, tuple]:
    """The pinned MIPS lane gate (dist/ghidra/GhidraMipsProfile.js): ELF32 ET_EXEC MIPS32r2 o32.

    The header-visible checks are enforced from the artifact bytes; the ABI-flags-record part of
    the upstream gate is satisfied by requiring the caller to declare the exact pinned lane (the
    extractor's inspected ABI record), mirroring upstream's "a family name alone does not select an
    ISA or ABI".
    """
    reasons = []
    if hdr is None or hdr.get("invalid") or hdr.get("machine") != ELF_MIPS_MACHINE:
        reasons.append("MIPS analysis requires ELF header metadata; a family name alone does not "
                       "select an ISA or ABI")
        return False, tuple(reasons)
    if declared_lane != MIPS_LANE:
        reasons.append(f"MIPS analysis on the pinned artifact requires the exact inspected lane "
                       f"{MIPS_LANE!r} (extractor-declared ABI flags); got {declared_lane!r}")
    if hdr["class"] != 32:
        reasons.append("this pinned MIPS lane supports ELF32 only; MIPS64/n64 remain unsupported")
    if hdr["type"] != 2:
        reasons.append("this pinned MIPS lane supports ET_EXEC only; relocatable objects, shared "
                       "images and PSP PRX need separate load verification")
    flags = hdr["flags"]
    if flags & 0x0F000000:
        reasons.append("MIPS16/microMIPS/ASE encodings are outside this standard-instruction lane")
    if flags & 0x00FF0000:
        reasons.append("machine-specific MIPS variants (e.g. PSP Allegrex) need a separately "
                       "supported processor profile")
    if flags & 0xF0000000 != 0x70000000:
        reasons.append("this pinned MIPS lane requires EF_MIPS_ARCH_32R2")
    if flags & 0x0000F000 != 0x00001000 or flags & 0x20:
        reasons.append("this pinned MIPS lane requires an explicit o32 ABI")
    if flags & 0x200:
        reasons.append("EF_MIPS_FP64 needs separate verification of 64-bit float registers")
    if flags & 0x400:
        reasons.append("EF_MIPS_NAN2008 needs separate verification of NaN encoding semantics")
    return (not reasons), tuple(reasons)


def host_admission(host_os: str) -> tuple[bool, tuple, tuple]:
    """Host matrix: macOS/Linux run the pinned Ghidra bridge."""
    if host_os in ("darwin", "macos", "linux"):
        return True, (), ()
    return False, (f"unsupported_host: REA 6.4.0 has no verified lane for host {host_os!r}"), ()


# ---------------------------------------------------------------- envelope parsing (P9.5)

@dataclass
class ToolOutcome:
    """One parsed tools/call result: success value or an error diagnostic, with any partial
    evidence (a retained evidence id) preserved separately — never discarded (E-03)."""

    ok: bool = False
    value: object = None              # canonical structured value ({"result": ...} unwrapped)
    diagnostic: str = ""              # error projection text (JSON) or protocol error text
    is_error: bool = False
    recovery_ref: str = ""            # retained evidence_id from recovery/partial_observation
    resource_hint: int = 0            # response_bytes_at_least when the transport budget was hit
    raw: dict | None = None


def _error_diagnostic(projected) -> str:
    if isinstance(projected, dict) and projected.get("error"):
        projected = projected["error"]
    try:
        return json.dumps(projected, sort_keys=True)
    except TypeError:
        return str(projected)


def parse_tool_result(payload: dict, tool: str) -> ToolOutcome:
    """Parse a tools/call response exactly like the pinned server encodes it (E-03).

    Success: ``structuredContent`` is canonical (falling back to the JSON text part); session tools
    unwrap ``{"result": ...}``. Errors: ``isError: true`` with a JSON diagnostic in text; a partial
    observation (``details.partial_observation``) or an oversized-completed analysis
    (``recovery.evidence_reference``) keeps its retained evidence id.
    """
    if "result" not in payload:
        err = payload.get("error") or {}
        return ToolOutcome(ok=False, is_error=True, raw=payload,
                           diagnostic=f"JSON-RPC error {err.get('code')}: {err.get('message')}")
    result = payload["result"]
    content = result.get("content") or []
    text = "".join(p.get("text", "") for p in content
                   if isinstance(p, dict) and p.get("type") == "text")
    if result.get("isError"):
        projected = {}
        if text.strip():
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    projected = parsed
            except json.JSONDecodeError:
                projected = {"raw_text": text}
        inner = projected.get("error", projected)
        details = inner.get("details") if isinstance(inner, dict) else None
        details = details if isinstance(details, dict) else {}
        recovery_ref, resource_hint = "", 0
        partial = details.get("partial_observation")
        recovery = details.get("recovery") or {}
        ref = (partial if isinstance(partial, dict) and partial.get("kind") == "retained-evidence"
               else (recovery.get("evidence_reference") if isinstance(recovery, dict) else None))
        if isinstance(ref, dict) and ref.get("kind") == "retained-evidence" \
                and EVIDENCE_ID_RE.match(str(ref.get("evidence_id", ""))):
            recovery_ref = str(ref["evidence_id"])
        if isinstance(details.get("response_bytes_at_least"), int):
            resource_hint = details["response_bytes_at_least"]
        return ToolOutcome(ok=False, is_error=True, diagnostic=_error_diagnostic(projected),
                           recovery_ref=recovery_ref, resource_hint=resource_hint, raw=result)
    value = result.get("structuredContent")
    if value is None and text.strip():
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return ToolOutcome(ok=False, is_error=True, raw=result,
                               diagnostic=f"malformed JSON text from {tool}")
    if isinstance(value, dict) and tool in REA_SESSION_TOOLS \
            and set(value) == {"result"}:
        value = value["result"]
    return ToolOutcome(ok=True, value=value, raw=result)


def validate_evidence_envelope(value, tool: str, digest: str, provider_id: str) -> dict:
    """Validate the Evidence envelope before using its content (P9.5); fail closed on drift."""
    if not isinstance(value, dict):
        raise ReaProviderFailure(f"REA {tool} returned a non-object envelope: "
                                 f"{type(value).__name__}")
    eid = value.get("evidence_id")
    if not isinstance(eid, str) or not EVIDENCE_ID_RE.match(eid):
        raise ReaProviderFailure(f"REA {tool} envelope has no valid evidence_id "
                                 f"(ev_ + 64 lowercase hex): {eid!r}")
    if "normalized_result" not in value:
        raise ReaProviderFailure(f"REA {tool} envelope {eid} has no normalized_result")
    provider = value.get("provider")
    if not isinstance(provider, dict) or provider.get("id") != provider_id:
        raise ReaProviderFailure(f"REA {tool} evidence {eid} reports provider "
                                 f"{provider!r}, expected id {provider_id!r}: identity conflict")
    operation = value.get("operation")
    if operation != tool:
        raise ReaProviderFailure(f"REA {tool} envelope {eid} records operation {operation!r}: "
                                 f"envelope mismatch")
    subject = value.get("subject")
    if subject is not None:
        subj_digest = ((subject.get("digest") or {}).get("sha256")
                       if isinstance(subject, dict) else None)
        if subj_digest is not None and not (isinstance(subj_digest, str)
                                            and SHA256_RE.match(subj_digest)):
            raise ReaProviderFailure(f"REA {tool} envelope {eid} has an invalid subject digest")
        if subj_digest is not None and subj_digest != digest:
            raise ReaArtifactIdentityError(
                f"REA {tool} evidence {eid} reports subject digest {subj_digest}, but the session "
                f"imported {digest}: identity conflict (the target changed since import)")
    return value


# ---------------------------------------------------------------- schema pin validation (E-01)

def _resolve_local_ref(schema: dict, prop: dict, depth: int = 4) -> dict:
    """Follow #/$defs/... references inside one inputSchema (the pinned server wraps properties)."""
    while depth and isinstance(prop, dict) and "$ref" in prop and not prop.get("type") \
            and not prop.get("enum") and not prop.get("anyOf") and not prop.get("oneOf"):
        ref = prop.get("$ref", "")
        if not ref.startswith("#/$defs/"):
            break
        node = (schema.get("$defs") or {}).get(ref.split("/")[-1])
        if not isinstance(node, dict):
            break
        prop = node
        depth -= 1
    return prop


def _allowed_values(prop: dict) -> set:
    values = set(prop.get("enum") or ())
    if "const" in prop:
        values.add(prop["const"])
    return values


def validate_schema_pin(catalog: dict) -> None:
    """Validate the live tools/list catalog against the pinned 6.4.0 constraints (E-01).

    Any drift raises :class:`ReaSchemaDrift` with the tool and the exact difference, so startup
    fails clearly instead of sending guessed arguments to a different schema.
    """
    for tool, pin in sorted(PINNED_TOOLS.items()):
        entry = catalog.get(tool)
        if entry is None:
            raise ReaSchemaDrift(f"schema drift: pinned tool {tool!r} is missing from the REA "
                                 f"{REA_PACKAGE_VERSION} catalog (live catalog has "
                                 f"{len(catalog)} tools)")
        schema = entry.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ReaSchemaDrift(f"schema drift: {tool} inputSchema is not an object schema: "
                                 f"{schema!r}")
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        if required != set(pin["required"]):
            raise ReaSchemaDrift(f"schema drift: {tool} requires {sorted(required)}, pinned "
                                 f"{sorted(pin['required'])}")
        for pname, constraint in pin.get("properties", {}).items():
            prop = _resolve_local_ref(schema, props.get(pname) or {})
            if not prop:
                raise ReaSchemaDrift(f"schema drift: {tool} lost pinned parameter {pname!r}")
            ptype = prop.get("type")
            if ptype is not None and constraint.get("type") and ptype != constraint["type"]:
                raise ReaSchemaDrift(f"schema drift: {tool}.{pname} is {ptype!r}, pinned "
                                     f"{constraint['type']!r}")
            if constraint.get("enum"):
                actual = _allowed_values(prop)
                if actual != set(constraint["enum"]):
                    raise ReaSchemaDrift(f"schema drift: {tool}.{pname} values "
                                         f"{sorted(actual)}, pinned "
                                         f"{sorted(constraint['enum'])}")
            if constraint.get("auto_allowed"):
                variants = [prop, *(prop.get("anyOf") or ()), *(prop.get("oneOf") or ())]
                if not any(isinstance(v, dict) and
                           (v.get("const") == "auto" or "auto" in (v.get("enum") or ()))
                           for v in variants):
                    raise ReaSchemaDrift(f"schema drift: {tool}.{pname} no longer allows the "
                                         f"'auto' provider selector")


# ---------------------------------------------------------------- stdio sidecar transport

@contextmanager
def _analysis_slot(timeout: float):
    """Hold one scheduler analysis slot for a single sidecar request. The sidecar session itself is
    not budgeted; a busy budget fails the request instead of queuing it."""
    from .. import scheduler  # lazy: avoids an import cycle with the scheduler
    try:
        with scheduler.BUDGET.slot("analysis", timeout=timeout):
            yield
    except TimeoutError as e:
        raise ReaProviderFailure("analysis budget busy") from e


class ReaMCPClient:
    """A private stdio JSON-RPC client over the pinned REA server (P9.3). Newline-delimited JSON,
    one request in flight at a time, bounded reads, MCP ``notifications/cancelled`` on cancel."""

    def __init__(self, command: list[str], *, cwd: Path | None = None, env: dict | None = None,
                 start_timeout: float = 60.0):
        self.command = list(command)
        self.cwd = cwd
        self.env = env if env is not None else allowed_env()
        self.start_timeout = start_timeout
        self.proc: subprocess.Popen | None = None
        self._next_id = 1
        self._lock = threading.Lock()
        self.started = threading.Event()   # set while a tools/call is in flight (test observable)
        self.idle = threading.Event()
        self.idle.set()
        self._closed = False

    # ---------------- lifecycle

    def start(self) -> None:
        self.proc = subprocess.Popen(self.command, cwd=self.cwd, env=self.env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1,
                                     start_new_session=True)

    def _write(self, payload: dict) -> None:
        if self.proc is None or self.proc.stdin is None or self.proc.poll() is not None:
            raise ReaProviderFailure("REA sidecar is not running (exited or never started)")
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _read_until(self, request_id: int, *, cancel: threading.Event | None, deadline: float,
                    on_cancel=None) -> dict | None:
        """Read lines until the response for request_id arrives; skip notifications. On cancel,
        emit notifications/cancelled once (the pinned server aborts the request); on deadline,
        return None."""
        cancelled_sent = False
        while True:
            if cancel is not None and cancel.is_set() and not cancelled_sent:
                cancelled_sent = True
                if on_cancel is not None:
                    try:
                        on_cancel(request_id)
                    except OSError:
                        pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if self.proc is None or self.proc.stdout is None or self.proc.poll() is not None:
                raise ReaProviderFailure("REA sidecar exited while a request was in flight")
            ready, _, _ = select.select([self.proc.stdout], [], [], min(0.05, remaining))
            if not ready:
                continue
            line = self.proc.stdout.readline()
            if not line:
                raise ReaProviderFailure("REA sidecar closed stdout while a request was in flight")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                raise ReaProviderFailure(f"REA sidecar emitted malformed JSON: {line[:200]!r}")
            if msg.get("id") == request_id:
                return msg
            # notifications (progress, logging, ...) are skipped

    def call(self, tool: str, arguments: dict, *, cancel: threading.Event | None = None,
             deadline: float | None = None) -> ToolOutcome:
        """One tools/call, parsed into a :class:`ToolOutcome`. Raises on transport failure."""
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self.started.set()
            self.idle.clear()
            try:
                self._write({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                             "params": {"name": tool, "arguments": arguments}})
                limit = deadline if deadline is not None else time.monotonic() + 3600.0

                def _send_cancel(rid: int) -> None:
                    self._write({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                 "params": {"requestId": rid, "reason": "client cancelled"}})

                response = self._read_until(request_id, cancel=cancel, deadline=limit,
                                            on_cancel=_send_cancel)
                if response is None:
                    raise ReaProviderFailure(f"REA {tool} did not respond within its deadline")
                return parse_tool_result(response, tool)
            except (BrokenPipeError, OSError) as e:
                raise ReaProviderFailure(f"REA sidecar transport failure on {tool}: {e}") from e
            finally:
                self.idle.set()

    def initialize(self, *, cancel: threading.Event | None = None) -> dict:
        """MCP initialize; validates the pinned server identity and protocol version (P9.1)."""
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": {
                "protocolVersion": REA_PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "decomp.wtf-rea-adapter", "version": "1.0.0"}}})
            deadline = time.monotonic() + self.start_timeout
            response = self._read_until(request_id, cancel=cancel, deadline=deadline)
            if response is None:
                raise ReaProviderFailure("REA did not answer initialize within the startup deadline")
            if "result" not in response:
                raise ReaProviderFailure(f"REA initialize failed: {response.get('error')}")
            result = response["result"]
            server = result.get("serverInfo") or {}
            if server.get("name") != REA_SERVER_NAME or server.get("version") != REA_PACKAGE_VERSION:
                raise ReaSchemaDrift(f"pinned artifact is rea-agents {REA_PACKAGE_VERSION}, but "
                                     f"the server identifies as {server.get('name')} "
                                     f"{server.get('version')}: re-pin before enabling the real "
                                     f"route")
            protocol = result.get("protocolVersion")
            if protocol != REA_PROTOCOL_VERSION:
                raise ReaSchemaDrift(f"REA negotiated protocol {protocol!r}, pinned "
                                     f"{REA_PROTOCOL_VERSION!r}")
            self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            return result

    def list_tools(self, *, cancel: threading.Event | None = None) -> dict:
        """tools/list -> {name: tool} (already parsed); validates the schema pin (E-01)."""
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": request_id, "method": "tools/list"})
            deadline = time.monotonic() + self.start_timeout
            response = self._read_until(request_id, cancel=cancel, deadline=deadline)
            if response is None:
                raise ReaProviderFailure("REA did not answer tools/list within the startup deadline")
            tools = (response.get("result") or {}).get("tools") or []
            catalog = {t.get("name"): t for t in tools if isinstance(t, dict) and t.get("name")}
            validate_schema_pin(catalog)
            return catalog

    def close(self) -> None:
        """Idempotent owned cleanup: terminate the process group, never a lingering half-session.

        The broker may close the session while a fetch still holds the client lock (cancellation
        path); the process is therefore terminated *before* any lock is taken, so a blocked reader
        unblocks with EOF instead of deadlocking on the lock.
        """
        if self._closed:
            return
        self._closed = True
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        if self._lock.acquire(blocking=False):  # polite shutdown only when no request is in flight
            try:
                if proc.stdin is not None:
                    try:
                        proc.stdin.write(json.dumps({"jsonrpc": "2.0",
                                                     "method": "shutdown"}) + "\n")
                        proc.stdin.flush()
                    except (BrokenPipeError, OSError, ValueError):
                        pass
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
            finally:
                self._lock.release()
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass
        self.started.clear()
        self.idle.set()


def allowed_env() -> dict:
    """Minimal allowlisted environment for the owned sidecar (P9.3), plus the shared Java/Ghidra
    discovery (JAVA_HOME and GHIDRA_INSTALL_DIR) so the pinned Ghidra bridge can launch."""
    env = {k: os.environ[k] for k in REA_ENV_ALLOWLIST if k in os.environ}
    java = ghidra.java_home()
    if java is not None:
        env["JAVA_HOME"] = str(java)
    gh = ghidra.ghidra_home()
    if gh is not None:
        env["GHIDRA_INSTALL_DIR"] = str(gh)
    return env


def default_command() -> tuple[list[str], Path]:
    """The pinned MCP entry: ``node scripts/rea.mjs mcp`` in the package root."""
    found = discover()
    if found is not None:
        node, root, entry = found
        return [node, str(root / entry), "mcp"], root
    node = shutil.which("node") or "node"
    return [node, str(REA_ROOT / REA_ENTRY), "mcp"], REA_ROOT


# ---------------------------------------------------------------- the provider adapter

class ReaProvider(Provider):
    """The broker's optional REA route: a pinned, read-only MCP sidecar per session (P9.3).

    ``name = "rea-ghidra"``; ``engine`` is the same underlying Ghidra identity the direct adapter
    reports, so both routes report one engine (A-04) and are never counted as independent
    confirmation.
    """

    name = "rea-ghidra"

    def __init__(self, *, command: list[str] | None = None, cwd: Path | None = None,
                 env: dict | None = None, host_os: str | None = None):
        default_cmd, default_cwd = default_command()
        self.command = list(command) if command is not None else default_cmd
        self.cwd = cwd if cwd is not None else default_cwd
        self.env = env if env is not None else allowed_env()
        self.host_os = (host_os or platform.system()).lower()
        # A-04: the SAME engine identity as DirectGhidraProvider (same call) so the broker's
        # correlation groups are shared between the direct and REA routes.
        self.engine = ghidra.provider_version()
        self._sessions: dict[str, dict] = {}

    # ------------------------------------------------------------ admission (P9.2/E-02)

    def capabilities(self, target_profile, artifact=None) -> CapabilityResult:
        path: Path | None = None
        data = b""
        if artifact is not None:
            try:
                path = Path(artifact).resolve()
                data = path.read_bytes()
            except OSError as e:
                return CapabilityResult(False, self.name,
                                        (f"artifact {artifact!r} cannot be read: {e}",))
        host_ok, host_reasons, _host_warnings = host_admission(self.host_os)
        if not host_ok:
            return CapabilityResult(False, self.name, host_reasons)
        if not path or not data:
            return CapabilityResult(False, self.name,
                                    ("REA admission requires the artifact bytes to pin the "
                                     "target identity (digest is re-verified at import)",))
        digest = hashlib.sha256(data).hexdigest()
        try:
            profile = _coerce_profile(target_profile)
        except ReaProviderFailure as e:
            return CapabilityResult(False, self.name, (str(e),))
        fmt = detect_format(data)
        if profile is None:  # auto: the pinned provider detects from headers, recorded as such
            if fmt is None:
                return CapabilityResult(False, self.name,
                                        ("auto admission needs a detectable header (elf/pe/mach-o/"
                                         "dos-mz); the pinned 6.4.0 has no general raw route",))
            return self._admitted_result(path, digest, fmt, "executable", None, None, {
                "auto": True, "format": fmt, "provider_id": REA_GHIDRA_PROVIDER_ID,
                "warnings": ["auto admission: the pinned provider selects language/loader from "
                             "the headers; the identity records only what admission saw"]})
        if profile.kind == "raw_region":
            return self._raw_admission(profile, path, data, digest)
        if profile.kind == "object":
            return CapabilityResult(False, self.name,
                                    ("the pinned 6.4.0 target model has no verified "
                                     "relocatable-object (ET_REL) lane; use the direct Ghidra "
                                     "adapter",))
        if profile.kind != "executable":
            return CapabilityResult(False, self.name,
                                    (f"unsupported target kind {profile.kind!r} for the REA "
                                     f"route; the pinned target model admits executable artifacts "
                                     f"and the dos-com raw lane",))
        if fmt is None:
            return CapabilityResult(False, self.name,
                                    ("headerless executable input has no pinned lane: only "
                                     "format='dos-com' (1..65280 bytes, explicitly declared) or "
                                     "headered artifacts are admitted",))
        if fmt == "dos-mz":
            return self._admitted_result(path, digest, "dos-mz", "executable", "x86", "little", {
                "provider_id": REA_GHIDRA_PROVIDER_ID,
                "mapping": "provider-owned: MzLoader, x86:LE:16:Real Mode, linear-byte-offset "
                           "coordinates (pinned artifact)"})
        if fmt == "elf":
            return self._elf_admission(profile, path, data, digest)
        if fmt == "pe":
            return self._pe_admission(profile, path, data, digest)
        if fmt == "mach-o":
            return self._macho_admission(path, data, digest)
        return CapabilityResult(False, self.name,
                                (f"unpinned container format {fmt!r} on the REA route "
                                 f"(archives/containers need the extractor plus a verified "
                                 f"supported representation; never guessed)",))

    def _raw_admission(self, profile, path, data, digest) -> CapabilityResult:
        declared = (profile.metadata or {}).get("rea_format")
        if declared == "dos-com":
            if not 1 <= len(data) <= DOS_COM_MAX_BYTES:
                return CapabilityResult(False, self.name,
                                        (f"DOS COM requires 1..{DOS_COM_MAX_BYTES} file bytes "
                                         f"(64 KiB segment minus the 256-byte PSP prefix), not "
                                         f"{len(data)}",))
            return self._admitted_result(path, digest, "dos-com", "executable", "x86", "little", {
                "provider_id": REA_GHIDRA_PROVIDER_ID,
                "mapping": "provider-owned: BinaryLoader, x86:LE:16:Real Mode, load segment "
                           "0x1000, entry offset 0x100 (pinned artifact); the caller's bytes are "
                           "never reinterpreted"})
        ok, reasons = raw_matrix_admission(
            profile.processor, profile.bits, profile.endian, profile.mode,
            (profile.metadata or {}).get("entry_point"))
        if not ok:
            return CapabilityResult(False, self.name, tuple(reasons))
        entry_point = (profile.metadata or {}).get("entry_point")
        mapping = {0: entry_point}  # caller-supplied entry_point IS the offset-zero mapping (R02)
        return CapabilityResult(False, self.name, (
            "rea_raw_route_absent: the documented raw-binary route (CPU family + entry_point "
            "mapping) is absent from the pinned rea-agents 6.4.0 MCP schema (open_binary takes "
            "path/format/provider_id/snapshot_path only); the profile above passes the "
            "documented matrix but cannot be issued — extend upstream deliberately or use the "
            "direct Ghidra adapter",),
            profile={"matrix": {"processor": profile.processor, "bits": profile.bits,
                                "endian": profile.endian, "mode": profile.mode,
                                "mapping": mapping, "status": "pinned_schema_absent"}})

    def _elf_admission(self, profile, path, data, digest) -> CapabilityResult:
        hdr = elf_header_info(data)
        if hdr is None or hdr.get("invalid"):
            return CapabilityResult(False, self.name, ("malformed ELF header",))
        declared = PROCESSOR_ALIASES.get((profile.processor or "").strip().lower())
        if profile.processor and declared is None:
            return CapabilityResult(False, self.name,
                                    (f"unknown profile processor {profile.processor!r}",))
        if profile.bits and hdr["class"] and profile.bits != hdr["class"]:
            return CapabilityResult(False, self.name,
                                    (f"the artifact is ELF{hdr['class']} but the profile says "
                                     f"{profile.bits}-bit",))
        if profile.endian and profile.endian != hdr["encoding"]:
            return CapabilityResult(False, self.name,
                                    (f"the artifact is {hdr['encoding']}-endian ELF but the "
                                     f"profile says {profile.endian}",))
        if hdr["machine"] == ELF_MIPS_MACHINE:
            ok, reasons = mips_lane_admission(hdr, (profile.metadata or {}).get("rea_mips_lane"))
            if not ok:
                return CapabilityResult(False, self.name, tuple(reasons))
            return self._admitted_result(path, digest, "elf", "executable", "mips",
                                         hdr["encoding"], {
                                             "provider_id": REA_GHIDRA_PROVIDER_ID,
                                             "mips_lane": MIPS_LANE,
                                             "byte_order": hdr["encoding"]})
        if hdr["machine"] in ELF_PPC_MACHINES:
            return CapabilityResult(False, self.name,
                                    ("no verified PowerPC ELF lane on the pinned 6.4.0 artifact "
                                     "for this adapter; use the direct Ghidra adapter (headered "
                                     "PPC admission is tested separately from the raw importer "
                                     "restrictions)",))
        if hdr["machine"] not in ELF_LE_MACHINES:
            return CapabilityResult(False, self.name,
                                    (f"ELF machine {hdr['machine']} has no verified lane on the "
                                     f"pinned REA artifact (verified: "
                                     f"{', '.join(sorted(ELF_LE_MACHINES.values()))}, plus the "
                                     f"pinned MIPS lane)",))
        arch = ELF_LE_MACHINES[hdr["machine"]]
        if declared is not None and declared not in (arch, "aarch64" if arch == "arm64" else arch):
            return CapabilityResult(False, self.name,
                                    (f"the ELF header identifies {arch} but the profile declares "
                                     f"{profile.processor}",))
        if hdr["encoding"] != "little":
            return CapabilityResult(False, self.name,
                                    (f"big-endian ELF (machine {hdr['machine']}) is outside the "
                                     f"verified little-endian lanes of the pinned artifact; the "
                                     f"reviewed route excludes it and it is never guessed",))
        return self._admitted_result(path, digest, "elf", "executable", arch, "little", {
            "provider_id": REA_GHIDRA_PROVIDER_ID,
            "language": "auto-from-header (pinned provider policy)"})

    def _pe_admission(self, profile, path, data, digest) -> CapabilityResult:
        hdr = pe_header_info(data)
        if hdr is None:
            return CapabilityResult(False, self.name, ("malformed PE header",))
        arch = PE_MACHINES.get(hdr["machine"])
        if arch is None:
            return CapabilityResult(False, self.name,
                                    (f"PE machine {hdr['machine']:#x} is outside the native "
                                     f"x86/x86-64 PE lane",))
        return self._admitted_result(path, digest, "pe", "executable", arch, "little", {
            "provider_id": REA_GHIDRA_PROVIDER_ID,
            "windows_boundary": {"host": self.host_os, "managed": hdr["managed"],
                                 "bits": hdr["bits"]},
            "warnings": ["PE target: the headless Ghidra bridge imports PE headers cross-platform"],
            "language": "auto-from-header (pinned provider policy)"})

    def _macho_admission(self, path, data, digest) -> CapabilityResult:
        hdr = macho_header_info(data)
        if hdr is None:
            return CapabilityResult(False, self.name, ("malformed Mach-O header",))
        if hdr["endian"] != "little" or hdr["cputype"] not in MACHO_CPUTYPES:
            return CapabilityResult(False, self.name,
                                    ("Mach-O on the pinned REA artifact is verified for "
                                     "little-endian x86_64/arm64 only; other slices are never "
                                     "guessed",))
        return self._admitted_result(path, digest, "mach-o", "executable",
                                     MACHO_CPUTYPES[hdr["cputype"]], "little", {
                                         "provider_id": REA_GHIDRA_PROVIDER_ID,
                                         "language": "auto-from-header (pinned provider policy)"})

    def _admitted_result(self, path, digest, fmt, kind, arch, endian, extra) -> CapabilityResult:
        identity = {"provider_id": REA_GHIDRA_PROVIDER_ID, "artifact": str(path), "digest": digest,
                    "format": fmt, "kind": kind, "architecture": arch, "endian": endian,
                    "pinned_package": {"name": REA_PACKAGE_NAME, "version": REA_PACKAGE_VERSION,
                                       "mcp_name": REA_MCP_NAME}}
        identity.update(extra or {})
        identity["snapshot_path"] = (extra or {}).get("snapshot_path")
        return CapabilityResult(True, self.name, (), profile=identity)

    # ------------------------------------------------------------ sessions (P9.3/P9.7)

    def open_session(self, session_key: str, profile: dict):
        """Start the owned sidecar, validate the schema pin, import the target, verify identity."""
        artifact = Path(profile.get("artifact", ""))
        digest = profile.get("digest", "")
        if not SHA256_RE.match(digest or ""):
            raise ReaArtifactIdentityError(f"session profile for {session_key} carries no pinned "
                                           f"artifact digest: admission did not pin identity")
        try:
            data = artifact.read_bytes()
        except OSError as e:
            raise ReaProviderFailure(f"REA cannot read {artifact}: {e}") from e
        current = hashlib.sha256(data).hexdigest()
        if current != digest:
            raise ReaArtifactIdentityError(
                f"artifact {artifact} changed between admission and import: admitted digest "
                f"{digest[:16]}... but current bytes hash to {current[:16]}... — the identity "
                f"conflict must be resolved by re-admission, never by using mismatched evidence")
        client = ReaMCPClient(self.command, cwd=self.cwd, env=self.env)
        try:
            client.start()
        except OSError as e:
            client.close()  # nothing was spawned, nothing is registered in self._sessions
            raise ReaProviderFailure(f"REA sidecar could not be started "
                                     f"({self.command[0] if self.command else '<empty command>'}): "
                                     f"{e}") from e
        try:
            with _analysis_slot(client.start_timeout):
                client.initialize()
            with _analysis_slot(client.start_timeout):
                client.list_tools()  # E-01: schema drift fails here, before any guessed arguments
            arguments = {"path": str(artifact), "provider_id": REA_GHIDRA_PROVIDER_ID}
            if profile.get("format") == "dos-com":
                arguments["format"] = "dos-com"
            if profile.get("snapshot_path"):
                arguments["snapshot_path"] = str(profile["snapshot_path"])
            with _analysis_slot(client.start_timeout):
                outcome = client.call("open_binary", arguments,
                                      deadline=time.monotonic() + client.start_timeout)
            if not outcome.ok:
                raise ReaProviderFailure(f"REA open_binary failed: {outcome.diagnostic}")
            facts = outcome.value
            if not isinstance(facts, dict):
                raise ReaProviderFailure("REA open_binary returned a non-object identity")
            if facts.get("sha256") != digest:
                raise ReaArtifactIdentityError(
                    f"REA imported {facts.get('sha256', '?')!r} but the target was admitted as "
                    f"{digest}: identity conflict (snapshot/profile mismatch is not silently "
                    f"accepted)")
            if facts.get("kind") != "executable":
                raise ReaProviderFailure(f"REA imported kind {facts.get('kind')!r}, expected "
                                         f"'executable'")
            if facts.get("format") not in REA_FORMATS:
                raise ReaProviderFailure(f"REA reported unknown format {facts.get('format')!r}")
            if facts.get("architecture") is not None and \
                    facts.get("architecture") not in REA_ARCHITECTURES:
                raise ReaProviderFailure(f"REA reported architecture "
                                         f"{facts.get('architecture')!r} outside the pinned "
                                         f"enum {REA_ARCHITECTURES}")
            handle = {"client": client, "artifact": artifact, "digest": digest, "profile": profile,
                      "facts": dict(facts), "stamp": (artifact.stat().st_size,
                                                      artifact.stat().st_mtime_ns),
                      "cache": {}, "lock": threading.Lock(), "invalid": False,
                      "invalid_reason": "", "snapshot_path": profile.get("snapshot_path"),
                      "session_key": session_key}
            self._sessions[session_key] = handle
            return handle
        except Exception:
            client.close()
            raise

    def close_session(self, session_key: str, handle) -> None:
        """Idempotent disposal: terminate the owned sidecar process group (ephemeral DB dropped)."""
        self._sessions.pop(session_key, None)
        client = handle.get("client") if isinstance(handle, dict) else None
        if isinstance(client, ReaMCPClient):
            client.close()

    # ------------------------------------------------------------ fetch (P9.4/P9.5)

    def fetch(self, session_key: str, handle, request: ProviderRequest,
              cancel: threading.Event | None) -> ProviderResult:
        client = handle["client"]
        if handle.get("invalid"):
            raise ReaSessionInvalidError(f"REA session {session_key} was invalidated "
                                         f"({handle.get('invalid_reason') or 'unknown reason'}): "
                                         f"recover by closing and reopening, never on a "
                                         f"half-valid session")
        artifact: Path = handle["artifact"]
        try:
            st = artifact.stat()
            if (st.st_size, st.st_mtime_ns) != handle.get("stamp"):
                current = hashlib.sha256(artifact.read_bytes()).hexdigest()
                if current != handle["digest"]:
                    raise ReaArtifactIdentityError(
                        f"target {artifact} changed since import (digest {current[:16]}... vs "
                        f"{handle['digest'][:16]}...): stale target identity, refusing to use "
                        f"mismatched evidence")
                handle["stamp"] = (st.st_size, st.st_mtime_ns)
        except OSError as e:
            raise ReaProviderFailure(f"REA cannot re-verify {artifact}: {e}") from e
        start = time.monotonic()
        budget = request.budget if request.budget is not None else Budget()
        deadline = start + budget.wall_seconds
        if cancel is not None and cancel.is_set():
            self._invalidate(handle, "cancelled before execution: the ephemeral database may be "
                                     "discarded by the provider")
            return ProviderResult(status="cancelled", wall_seconds=0.0, worker_seconds=0.0,
                                  diagnostics=["cancelled before execution"])
        try:
            functions: dict[str, FunctionEvidence] = {}
            refs: dict[str, EvidenceRef] = {}
            diagnostics: list[str] = []
            new_any = False
            for name in request.functions:
                if cancel is not None and cancel.is_set():
                    self._invalidate(handle, "cancelled during fetch")
                    return ProviderResult(status="cancelled",
                                          wall_seconds=time.monotonic() - start,
                                          worker_seconds=time.monotonic() - start,
                                          diagnostics=["cancelled during fetch"])
                if budget.exhausted():
                    self._invalidate(handle, "budget exhausted mid-request")
                    return ProviderResult(status="budget_exceeded",
                                          wall_seconds=time.monotonic() - start,
                                          worker_seconds=time.monotonic() - start,
                                          diagnostics=["request budget exhausted; no partial "
                                                       "result is reported as complete"])
                key = f"{request.inputs.get('module', '')}::{name}"
                cache_key = (request.snapshot, tuple(sorted(request.facets)), name,
                             str(request.inputs.get("module", "")))
                with handle["lock"]:
                    cached = handle["cache"].get(cache_key)
                if cached is not None:
                    functions[key] = cached["evidence"]
                    refs.update(cached["refs"])
                    continue
                new_any = True
                wall = time.monotonic()
                evidence, function_refs, func_diagnostics = self._function_evidence(
                    client, handle, request, name, key, cancel, deadline)
                budget.spend(wall=time.monotonic() - wall)
                if cancel is not None and cancel.is_set():
                    # the cancellation discarded the ephemeral database (pinned semantics)
                    self._invalidate(handle, "cancelled during fetch")
                    return ProviderResult(status="cancelled",
                                          wall_seconds=time.monotonic() - start,
                                          worker_seconds=time.monotonic() - start,
                                          diagnostics=["cancelled during fetch"])
                functions[key] = evidence
                refs.update(function_refs)
                diagnostics.extend(func_diagnostics)
                with handle["lock"]:
                    handle["cache"][cache_key] = {"evidence": evidence,
                                                  "refs": dict(function_refs)}
            status = "ok" if not functions else self._overall_status(functions)
            wall = time.monotonic() - start
            return ProviderResult(status=status, functions=functions, refs=refs,
                                  diagnostics=diagnostics, wall_seconds=wall,
                                  worker_seconds=wall, reused=bool(functions) and not new_any)
        except ReaSessionInvalidError:
            raise
        except ReaProviderFailure:
            raise
        except Exception as e:  # transport/protocol failures fail the session (retryable)
            raise ReaProviderFailure(f"REA fetch failed for {request.function_key!r}: {e}") from e

    @staticmethod
    def _overall_status(functions: dict[str, FunctionEvidence]) -> str:
        if all(f.status == "failed" for f in functions.values()):
            return "failed"
        if any(f.status not in ("ok", "empty", "unknown") for f in functions.values()):
            return "partial"
        return "ok"

    def _function_evidence(self, client: ReaMCPClient, handle: dict, request: ProviderRequest,
                           name: str, key: str, cancel, deadline: float) \
            -> tuple[FunctionEvidence, dict, list]:
        limits = {**{"max_pseudocode": 8000, "max_instructions": 200, "max_blocks": 200,
                     "max_calls": 100, "max_refs": 200}, **request.limits}
        facets = request.facets
        diagnostics: list[str] = []
        refs: dict[str, EvidenceRef] = {}
        address = self._resolve_procedure(client, name, cancel, deadline, handle["digest"])
        if address is None:
            coverage = {f: ("failed" if f == "pseudocode" else "unknown") for f in FACETS}
            return (FunctionEvidence(function_key=key, provider=self.name, engine=self.engine,
                                     correlation_group="", snapshot=request.snapshot, status="failed",
                                     coverage=coverage,
                                     diagnostics=[f"no procedure for {name!r} (not an observed "
                                                  f"absence: the name simply did not resolve)"]),
                    {}, [])
        with _analysis_slot(client.start_timeout):
            outcome = client.call("analyze_function", {"procedure": address}, cancel=cancel,
                                  deadline=deadline)
        if not outcome.ok:
            if outcome.recovery_ref:
                # E-03/E-04: a partial observation or an oversized-but-completed analysis keeps
                # its retained evidence id; the complete evidence is never dropped or truncated.
                ref = f"rea:{handle['session_key']}:{outcome.recovery_ref}"
                coverage = {f: ("partial" if f == "pseudocode" else "unknown") for f in FACETS}
                if outcome.resource_hint:
                    guidance = (f"analyze_function output exceeded the MCP transport budget; the "
                                f"complete evidence is retained at {outcome.recovery_ref} "
                                f"(~{outcome.resource_hint} bytes).")
                else:
                    guidance = (f"analyze_function failed after recording partial evidence "
                                f"retained at {outcome.recovery_ref}: {outcome.diagnostic}")
                diagnostics.append(f"{guidance} Nothing was truncated: use the retained view "
                                   f"(inspect_analysis_view) or export_evidence_bundle to "
                                   f"consume it.")
                refs[key] = EvidenceRef(ref=ref, facet="dossier", size_hint=outcome.resource_hint,
                                        summary=f"{name}: retained oversized dossier")
                return (FunctionEvidence(function_key=key, provider=self.name, engine=self.engine,
                                         correlation_group="", snapshot=request.snapshot,
                                         status="partial", coverage=coverage, views={}, refs=refs,
                                         diagnostics=diagnostics), refs, diagnostics)
            coverage = {f: ("failed" if f == "pseudocode" else "unknown") for f in FACETS}
            diagnostics.append(f"analyze_function failed for {name!r}: {outcome.diagnostic}")
            return (FunctionEvidence(function_key=key, provider=self.name, engine=self.engine,
                                     correlation_group="", snapshot=request.snapshot, status="failed",
                                     coverage=coverage, diagnostics=diagnostics), refs, diagnostics)
        dossier = outcome.value
        validate_evidence_envelope(dossier, "analyze_function", handle["digest"],
                                   REA_GHIDRA_PROVIDER_ID)
        evidence_id = dossier.get("evidence_id") if isinstance(dossier, dict) else ""
        normalized = dossier.get("normalized_result") if isinstance(dossier, dict) else None
        if not isinstance(normalized, dict):
            raise ReaProviderFailure(f"REA analyze_function for {name!r} returned an invalid "
                                     f"normalized_result: {type(normalized).__name__}")
        instructions_view = None
        if "instructions" in facets:
            with _analysis_slot(client.start_timeout):
                inst_outcome = client.call("read_function_instructions", {"procedure": address},
                                           cancel=cancel, deadline=deadline)
            instructions_view = self._instructions_view(inst_outcome, handle)
        views, coverage, entry, classification = self._project_dossier(
            normalized, facets, limits, instructions_view)
        if evidence_id:
            refs[key] = EvidenceRef(ref=f"rea:{handle['session_key']}:{evidence_id}",
                                    facet="dossier", size_hint=len(json.dumps(normalized)),
                                    summary=name)
        status = coverage.get("pseudocode", "unknown")
        return (FunctionEvidence(function_key=key, provider=self.name, engine=self.engine,
                                 correlation_group="", snapshot=request.snapshot, status=status,
                                 coverage=coverage, views=views, refs=refs,
                                 diagnostics=diagnostics, classification=classification,
                                 entry=entry), refs, diagnostics)

    def _resolve_procedure(self, client, name: str, cancel, deadline: float,
                           digest: str) -> str | None:
        with _analysis_slot(client.start_timeout):
            outcome = client.call("procedure_address", {"procedure": name}, cancel=cancel,
                                  deadline=deadline)
        if not outcome.ok:
            return None
        validate_evidence_envelope(outcome.value, "procedure_address", digest,
                                   REA_GHIDRA_PROVIDER_ID)
        normalized = outcome.value.get("normalized_result") if isinstance(outcome.value, dict) else None
        if not isinstance(normalized, str) or not normalized.strip():
            return None
        return normalized

    def _instructions_view(self, outcome: ToolOutcome, handle: dict):
        """read_function_instructions -> a bounded view; None/null stays 'unavailable' (E-09)."""
        if not outcome.ok:
            return {"status": "failed", "reason": outcome.diagnostic}
        validate_evidence_envelope(outcome.value, "read_function_instructions", handle["digest"],
                                   REA_GHIDRA_PROVIDER_ID)
        normalized = outcome.value.get("normalized_result") if isinstance(outcome.value, dict) else None
        if normalized is None:
            return {"status": "unavailable",
                    "reason": "the provider returned null instructions (unavailable stays "
                              "distinct from empty)"}
        if not isinstance(normalized, dict):
            return {"status": "failed", "reason": "malformed instructions result"}
        items = normalized.get("instructions")
        if items is None:
            return {"status": "unavailable", "reason": "instructions facet absent from the result"}
        if not isinstance(items, list):
            return {"status": "failed", "reason": "instructions is not a list"}
        if not items:
            return {"status": "empty", "items": []}
        return {"status": "ok", "items": [{"asm": str(i)} for i in items[:50]],
                "count": len(items), "truncated": len(items) > 50}

    def _project_dossier(self, d: dict, facets, limits: dict, instructions_view):
        """Project the pinned dossier onto broker facets; null/absent stays unavailable, [] stays
        empty, unrequested stays unknown (E-09)."""
        views: dict = {}
        coverage: dict = {facet: "unknown" for facet in FACETS}
        if "pseudocode" in facets:
            text = d.get("pseudocode")
            if text is None:
                coverage["pseudocode"] = "unavailable"
                views["pseudocode"] = {"status": "unavailable",
                                       "reason": "provider returned null pseudocode (procedure_"
                                                 "pseudo_code may return null; unavailable is "
                                                 "not empty)"}
            elif text == "":
                coverage["pseudocode"] = "empty"
                views["pseudocode"] = {"status": "empty", "text": ""}
            else:
                coverage["pseudocode"] = "ok"
                truncated = len(text) > limits["max_pseudocode"]
                views["pseudocode"] = {"status": "ok", "warnings": list(d.get("limitations") or []),
                                       "text": text[:limits["max_pseudocode"]],
                                       "truncated": truncated}
        if "instructions" in facets:
            coverage["instructions"] = (instructions_view or {}).get("status", "unavailable")
            if instructions_view is not None:
                views["instructions"] = instructions_view
        if "blocks" in facets:
            blocks = d.get("basic_blocks")
            if blocks is None:
                coverage["blocks"] = "unavailable"
                views["blocks"] = {"status": "unavailable", "reason": "basic_blocks absent"}
            elif not blocks:
                coverage["blocks"] = "empty"
                views["blocks"] = {"status": "empty", "items": []}
            else:
                coverage["blocks"] = "ok"
                items = [{"start": b.get("start"), "end": b.get("end"),
                          "successors": list(b.get("successors") or [])}
                         for b in blocks[:limits["max_blocks"]] if isinstance(b, dict)]
                views["blocks"] = {"items": items,
                                   "truncated": len(blocks) > limits["max_blocks"]}
        if "calls" in facets:
            callees = d.get("callees")
            if callees is None:
                coverage["calls"] = "unavailable"
                views["calls"] = {"status": "unavailable", "reason": "callees absent"}
            else:
                items = []
                for c in callees:
                    if not isinstance(c, dict):
                        continue
                    classification = c.get("classification") or {}
                    items.append({"address": c.get("address"), "name": c.get("name", ""),
                                  "indirect": False,
                                  "external": bool(classification.get("external")),
                                  "thunk": bool(classification.get("thunk"))})
                unresolved = [{"address": u.get("address"), "reason": u.get("reason")}
                              for u in (d.get("unresolved_calls") or []) if isinstance(u, dict)]
                views["calls"] = {"items": items[:limits["max_calls"]],
                                  "truncated": len(items) > limits["max_calls"],
                                  "unresolved": unresolved}
                coverage["calls"] = "empty" if not items and not unresolved else "ok"
        if "references" in facets:
            incoming = d.get("incoming_references")
            outgoing = d.get("outgoing_references")
            if incoming is None and outgoing is None:
                coverage["references"] = "unavailable"
                views["references"] = {"status": "unavailable",
                                       "reason": "reference edges absent"}
            else:
                items = []
                for direction, edges in (("incoming", incoming), ("outgoing", outgoing)):
                    for e in edges or []:
                        if not isinstance(e, dict):
                            continue
                        kind = e.get("kind") or {}
                        items.append({"address": e.get("source_address"),
                                      "target": e.get("target_address"),
                                      "direction": direction,
                                      "kind": "available" if kind.get("available") else
                                              "unavailable"})
                views["references"] = {"items": items[:limits["max_refs"]],
                                       "truncated": len(items) > limits["max_refs"]}
                coverage["references"] = "empty" if not items else "ok"
        if "types" in facets:
            # GAP-2: the pinned catalog has no type-inventory tool; never guess one.
            coverage["types"] = "unavailable"
            views["types"] = {"status": "unavailable",
                              "reason": "no type-inventory tool in the pinned 6.4.0 catalog "
                                        "(inspect_native_data_type needs an explicit address and "
                                        "is not a facet inventory)"}
        if "pcode" in facets:
            flow = d.get("native_value_flow")
            if flow is None:
                coverage["pcode"] = "unavailable"
                views["pcode"] = {"status": "unavailable",
                                  "reason": "native_value_flow is null (unavailable, distinct "
                                            "from an observed empty flow)"}
            elif not flow:
                coverage["pcode"] = "empty"
                views["pcode"] = {"status": "empty", "items": []}
            else:
                coverage["pcode"] = "ok"
                views["pcode"] = {"status": "ok", "flow": flow}
        procedure = d.get("procedure") or {}
        classification = ""
        cls = procedure.get("classification") or {}
        if cls.get("external"):
            classification = "external"
        elif cls.get("thunk"):
            classification = "thunk"
        entry = None
        addr = procedure.get("address")
        if isinstance(addr, str) and addr.lower().startswith("0x"):
            try:
                entry = int(addr, 16)
            except ValueError:
                entry = None
        return views, coverage, entry, classification

    @staticmethod
    def _invalidate(handle: dict, reason: str) -> None:
        with handle["lock"]:
            handle["invalid"] = True
            handle["invalid_reason"] = reason

    # ------------------------------------------------------------ invalidation (P9.6/E-06)

    def refresh(self, session_key: str, scope: str, new_snapshot: str) -> int:
        """A context/snapshot change: drop cached observations for the scope; return the count.

        A snapshot replay is eligible observations only — after refresh the next request is a
        genuinely new query and goes back to the provider (a fresh import may be required; the
        adapter never claims restart-free whole-program analysis from a snapshot file).
        """
        handle = self._sessions.get(session_key)
        if not isinstance(handle, dict):
            return 0
        module = scope.split("::", 1)[0]
        removed = 0
        with handle["lock"]:
            for cache_key in list(handle["cache"]):
                # cache_key = (snapshot, facets, name, module)
                if scope == "*" or cache_key[3] == module:
                    handle["cache"].pop(cache_key, None)
                    removed += 1
        return removed

    def flush(self, session_key: str, *, view_invalidated: bool, reason: str) -> None:
        """flushCache()-style invalidation: metadata changes drop cached interpretations; a
        view invalidation (overlays) additionally invalidates the whole sidecar session."""
        handle = self._sessions.get(session_key)
        if not isinstance(handle, dict):
            return
        with handle["lock"]:
            handle["cache"].clear()
            if view_invalidated:
                self._invalidate(handle, f"view invalidated: {reason or 'reset/reimport required'}")


def _coerce_profile(profile):
    """Accept TargetProfile | dict | None (the broker stores the original profile object)."""
    if profile is None or isinstance(profile, ghidra.TargetProfile):
        return profile
    if isinstance(profile, dict):
        fields = {f.name for f in ghidra.TargetProfile.__dataclass_fields__.values()}
        unknown = sorted(set(profile) - fields)
        if unknown:
            raise ReaProviderFailure(f"unknown profile field(s) for the REA route: "
                                     f"{', '.join(unknown)}")
        return ghidra.TargetProfile(**profile)
    raise ReaProviderFailure(f"profile must be a TargetProfile or dict, not "
                             f"{type(profile).__name__}")
