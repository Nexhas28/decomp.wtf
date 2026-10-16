"""REA provider adapter tests (plan E-01..E-09).

A tiny fake stdio MCP server (a python subprocess) stands in for the pinned rea-agents 6.4.0
install for transport tests; the schema the fake serves is built from the adapter's own
:data:`~decompnet.analysis.rea.PINNED_TOOLS` pin, and drift scenarios mutate it (E-01). One gated
real-lane test drives the actual install read-only (initialize + tools/list with an isolated
HOME/TMPDIR; no artifact is opened) and is skipped when the install is absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from decompnet.analysis import rea
from decompnet.analysis.broker import (Broker, DirectGhidraProvider, Provider, ProviderError,
                                       independent_sources)
from decompnet.analysis.contracts import (Budget, CapabilityResult, FunctionEvidence,
                                          ProviderRequest, ProviderResult)
from decompnet.analysis.rea import (DOS_COM_MAX_BYTES, PINNED_TOOLS, ReaMCPClient, ReaProvider,
                                    elf_header_info, mips_lane_admission, parse_tool_result,
                                    raw_matrix_admission, validate_evidence_envelope)
from decompnet.forks.ghidra import TargetProfile

# ---------------------------------------------------------------- shared helpers

FAKE_SERVER = r'''
import json, os, select, signal, sys, hashlib

SCENARIO = os.environ.get("REA_SCENARIO", "default")
VERSION = "6.4.1" if SCENARIO == "wrong-version" else "6.4.0"


def state(name, default=None):
    try:
        return json.load(open(os.path.join(os.getcwd(), name)))
    except Exception:
        return default


def save(name, obj):
    json.dump(obj, open(os.path.join(os.getcwd(), name), "w"))


def log(line):
    with open(os.path.join(os.getcwd(), "calls.log"), "a") as f:
        f.write(line + "\n")


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def eid(seed):
    return "ev_" + hashlib.sha256(("fake:" + seed).encode()).hexdigest()


def evidence(tool, normalized, name="", digest=""):
    subject = None
    if digest:
        subject = {"name": name, "digest": {"sha256": digest}, "format": "elf",
                   "architecture": "x86_64", "local_path": ""}
    return {
        "evidence_id": eid(tool + ":" + name + ":" + digest),
        "subject": subject,
        "provider": {"id": "ghidra", "name": "Ghidra", "version": None},
        "analysis_profile": None,
        "predicate_type": "observation",
        "operation": tool,
        "parameters": {"procedure": name},
        "raw_result": None,
        "normalized_result": normalized,
        "confidence": "observed",
        "authority": "shipped-artifact",
        "environment": None,
        "limitations": ["fake limitation"],
        "locations": [],
        "evidence_links": [],
    }


def error_projection(code, message="fake failure", details=None):
    inner = {"code": code, "message": message}
    if details:
        inner["details"] = details
    return {"error": inner}


def tool_result(payload):
    text = json.dumps(payload)
    return {"jsonrpc": "2.0", "content": [{"type": "text", "text": text}],
            "structuredContent": payload}


def is_error_result(projected):
    text = json.dumps(projected)
    return {"jsonrpc": "2.0", "content": [{"type": "text", "text": text}],
            "structuredContent": projected, "isError": True}


def catalog():
    tools = json.load(open(os.path.join(os.getcwd(), "catalog.json")))
    if SCENARIO == "missing-tool":
        tools = [t for t in tools if t["name"] != "analyze_function"]
    if SCENARIO == "wrong-required":
        for t in tools:
            if t["name"] == "open_binary":
                t["inputSchema"]["required"] = ["path", "extra_guessed_param"]
    if SCENARIO == "wrong-enum":
        for t in tools:
            if t["name"] == "get_call_graph":
                t["inputSchema"]["properties"]["direction"]["enum"] = ["left", "right"]
    if SCENARIO == "no-auto":
        for t in tools:
            if t["name"] == "open_binary":
                t["inputSchema"]["properties"]["provider_id"] = {
                    "type": "string", "enum": ["ghidra", "hopper"]}
    return tools


def proc_identity(address, name):
    return {"address": address, "name": name, "classification": None,
            "body": {"available": True, "provenance": "ghidra",
                     "ranges": [{"start": address, "end": address}],
                     "total_bytes": 4, "span_bytes": 4, "non_contiguous": False,
                     "contains_entry": True}}


def dossier(address, name, digest, with_pcode=False):
    callees = ([proc_identity("0x2000", "callee1")] if name == "primary" else [])
    flow = {"defs": [{"address": address}]} if with_pcode else None
    return {
        "procedure": {**proc_identity(address, name), "signature": None, "locals": []},
        "pseudocode": f"int {name}(void) {{ return 0; }}",
        "assembly": ["mov eax, 1", "ret"],
        "comments": [],
        "callers": [],
        "callees": callees,
        "incoming_references": [],
        "outgoing_references": [],
        "referenced_strings": [],
        "referenced_names": [],
        "basic_blocks": [{"start": address, "end": address, "successors": []}],
        "native_api": None,
        "native_value_flow": flow,
        "limitations": ["fake limitation"],
    }


digest = ""
last_address = {"0x1000": "f1"}
pending_block = None

while True:
    line = sys.stdin.readline()
    if not line:
        break
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"],
              "result": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "serverInfo": {"name": "rea", "version": VERSION}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": catalog()}})
    elif method == "notifications/cancelled":
        if pending_block is not None:
            rid, blocked_name = pending_block
            pending_block = None
            open(os.path.join(os.getcwd(), "cancel_ack"), "w").close()
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"content": [{"type": "text", "text": json.dumps(
                      error_projection("cancelled", "cancelled by client"))}],
                             "structuredContent": error_projection("cancelled"),
                             "isError": True}})
    elif method == "tools/call":
        params = msg["params"]
        name, args = params["name"], params.get("arguments") or {}
        log(name)
        if name == "open_binary":
            log(f"open_binary:snapshot={args.get('snapshot_path', '')}")
            import pathlib
            data = pathlib.Path(args["path"]).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            reported = "0" * 64 if SCENARIO == "identity-mismatch" else digest
            send({"jsonrpc": "2.0", "id": msg["id"], "result": tool_result(
                {"result": {"path": args["path"], "format": "elf", "kind": "executable",
                            "sha256": reported, "architecture": "x86_64"}})})
        elif name == "procedure_address":
            if SCENARIO == "missing-function" and args.get("procedure") == "f1":
                send({"jsonrpc": "2.0", "id": msg["id"], "result": is_error_result(
                    error_projection("name_not_found", "no procedure named f1"))})
            else:
                last_address["0x1000"] = args.get("procedure", "f1")
                send({"jsonrpc": "2.0", "id": msg["id"], "result": tool_result(
                    evidence("procedure_address", "0x1000",
                             args.get("procedure", ""), digest))})
        elif name == "analyze_function":
            fname = last_address.get(args.get("procedure"), "f1")
            if SCENARIO == "block-once" and pending_block is None \
                    and not os.path.exists(os.path.join(os.getcwd(), "blocked")):
                open(os.path.join(os.getcwd(), "blocked"), "w").close()
                open(os.path.join(os.getcwd(), "blocking"), "w").close()
                pending_block = (msg["id"], fname)
                while pending_block is not None:
                    ready, _, _ = select.select([sys.stdin], [], [], 0.1)
                    if not ready:
                        continue
                    inner = sys.stdin.readline()
                    if not inner:
                        break
                    inner_msg = json.loads(inner)
                    if inner_msg.get("method") == "notifications/cancelled":
                        rid, _ = pending_block
                        pending_block = None
                        open(os.path.join(os.getcwd(), "cancel_ack"), "w").close()
                        send({"jsonrpc": "2.0", "id": rid,
                              "result": {"content": [{"type": "text", "text": json.dumps(
                                  error_projection("cancelled", "cancelled by client"))}],
                                         "structuredContent": error_projection("cancelled"),
                                         "isError": True}})
                        break
            elif SCENARIO == "kill-mid-request":
                os.kill(os.getpid(), signal.SIGKILL)
            elif SCENARIO == "malformed-output":
                sys.stdout.write("{this is not json\n")
                sys.stdout.flush()
            elif SCENARIO == "oversize":
                send({"jsonrpc": "2.0", "id": msg["id"], "result": is_error_result(
                    error_projection("resource_constraint",
                                     "complete MCP response exceeds the stdio budget",
                                     {"recovery": {"evidence_reference": {
                                         "kind": "retained-evidence",
                                         "evidence_id": eid("oversize")}},
                                      "response_bytes_at_least": 512345}))})
            elif SCENARIO == "error-f2" and fname == "f2":
                send({"jsonrpc": "2.0", "id": msg["id"], "result": is_error_result(
                    error_projection("boom", "bad function"))})
            elif SCENARIO == "partial-evidence":
                send({"jsonrpc": "2.0", "id": msg["id"], "result": is_error_result(
                    error_projection("partial", "failed after recording",
                                     {"partial_observation": {
                                         "kind": "retained-evidence",
                                         "evidence_id": eid("partial-obs")}}))})
            else:
                send({"jsonrpc": "2.0", "id": msg["id"], "result": tool_result(
                    evidence("analyze_function",
                             dossier(args.get("procedure"), fname, digest,
                                     with_pcode=(SCENARIO == "with-pcode")),
                             fname, digest))})
        elif name == "read_function_instructions":
            if SCENARIO == "instructions-null":
                normalized = None
            else:
                normalized = {"procedure": proc_identity("0x1000", "f1"),
                              "instructions": ["mov eax, 1", "ret"],
                              "limitations": ["fake"]}
            send({"jsonrpc": "2.0", "id": msg["id"], "result": tool_result(
                evidence("read_function_instructions", normalized, "f1", digest))})
        elif name == "export_evidence_bundle" and SCENARIO == "block-export":
            open(os.path.join(os.getcwd(), "blocking"), "w").close()
            pending_block = (msg["id"], name)
        else:
            send({"jsonrpc": "2.0", "id": msg["id"], "result": is_error_result(
                error_projection("unsupported", f"fake server has no {name}"))})
'''


def wait_for(predicate, timeout=10.0):
    """Bounded, event-driven wait: poll a cheap predicate at 5ms (no unbounded sleeps)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def pinned_catalog() -> list[dict]:
    """A tools/list catalog whose schemas satisfy the adapter's exact pin (E-01 baseline)."""
    tools = []
    for name, pin in sorted(PINNED_TOOLS.items()):
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        for pname, constraint in pin.get("properties", {}).items():
            prop = {"type": constraint.get("type") or "object"}
            if pname == "format" and name == "open_binary":  # exercise the $ref/const shape
                schema["$defs"] = {**schema.get("$defs", {}),
                                   "fmt": {"type": "string", "const": "dos-com"}}
                prop = {"$ref": "#/$defs/fmt"}
            elif constraint.get("enum"):
                prop["enum"] = list(constraint["enum"])
            elif constraint.get("auto_allowed"):
                prop = {"type": "string",
                        "anyOf": [{"type": "string", "const": "auto"},
                                  {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"}]}
            if pname == "overwrite":
                prop = {"type": "boolean", "default": False}
            schema["properties"][pname] = prop
        schema["required"] = list(pin["required"])
        tools.append({"name": name, "inputSchema": schema,
                      "description": f"fake pinned tool {name}"})
    return tools


def elf64_le(machine: int = 62, enc: int = 1, etype: int = 2, flags: int = 0) -> bytes:
    """A minimal 64-bit ELF header; all multi-byte fields use the file's own data encoding."""
    endian = "little" if enc == 1 else "big"
    data = bytearray(b"\x7fELF" + bytes([2, enc, 1]) + bytes(9))
    data += etype.to_bytes(2, endian) + machine.to_bytes(2, endian) + bytes(4)
    data += bytes(8) + bytes(8) + bytes(8)          # e_entry, e_phoff, e_shoff
    data += flags.to_bytes(4, endian)               # e_flags (64-bit: offset 48)
    data += bytes(16)
    return bytes(data)


def elf32_mips(flags: int = 0x70001000, etype: int = 2) -> bytes:
    data = bytearray(b"\x7fELF" + bytes([1, 1, 1]) + bytes(9))
    data += etype.to_bytes(2, "little") + (8).to_bytes(2, "little") + bytes(4)
    data += bytes(4) + bytes(4) + bytes(4)          # e_entry, e_phoff, e_shoff
    data += flags.to_bytes(4, "little")             # e_flags (32-bit: offset 36)
    data += bytes(16)
    return bytes(data)


def pe_image(machine: int = 0x8664, managed: bool = False) -> bytes:
    data = bytearray(b"MZ" + bytes(0x3A))
    data[0x3C:0x40] = (0x40).to_bytes(4, "little")
    data += b"PE\x00\x00"
    data += machine.to_bytes(2, "little") + (1).to_bytes(2, "little") + bytes(16)
    opt = len(data)
    data += (0x10B).to_bytes(2, "little") + bytes(94)   # PE32 optional header up to data dirs
    dd = opt + 96
    data += bytes(16 * 16)                              # data directories
    if managed:
        data[dd + 14 * 8:dd + 14 * 8 + 4] = (0x2000).to_bytes(4, "little")  # COM descriptor RVA
    return bytes(data)


def macho_x86_64() -> bytes:
    return b"\xcf\xfa\xed\xfe" + (0x01000007).to_bytes(4, "little") + bytes(64)


def make_profile(**kw) -> TargetProfile:
    base = {"kind": "executable", "processor": "x86_64", "bits": 64, "endian": "little",
            "mode": "", "metadata": {}}
    base.update(kw)
    return TargetProfile(**base)


def make_request(functions=("f1",), facets=("pseudocode",), artifact="", snapshot="current",
                 module="mod1", inputs=None) -> ProviderRequest:
    return ProviderRequest(request_id="r1", kind="function", function_key=f"{module}::{functions[0]}",
                           functions=functions, facets=facets, snapshot=snapshot,
                           inputs=inputs or {"artifact": artifact, "module": module})


@pytest.fixture
def fake(tmp_path):
    """A fake stdio MCP server in its own state dir; returns (provider_factory, state_dir)."""
    def make(scenario="default"):
        state_dir = tmp_path / f"state-{scenario}-{os.getpid()}"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "server.py").write_text(FAKE_SERVER)
        (state_dir / "catalog.json").write_text(json.dumps(pinned_catalog()))
        (state_dir / "calls.log").write_text("")
        env = {"REA_SCENARIO": scenario, "HOME": str(state_dir), "TMPDIR": str(state_dir),
               "PATH": os.environ.get("PATH", ""), "LANG": "C"}

        def provider(**kw):
            return ReaProvider(command=[sys.executable, str(state_dir / "server.py")],
                               cwd=state_dir, env=env, host_os="darwin", **kw)
        return provider, state_dir
    return make


@pytest.fixture
def elf_artifact(tmp_path):
    p = tmp_path / "target.bin"
    p.write_bytes(elf64_le())
    return p


def admitted(provider, artifact: Path, **kw) -> dict:
    result = provider.capabilities(make_profile(**kw), artifact)
    assert result.supported, result.reasons
    return dict(result.profile)


def session_fetch(provider, profile, artifact, *args, **kw):
    """open_session + fetch + close_session as a self-contained round trip."""
    handle = provider.open_session("s1", profile)
    try:
        return provider.fetch("s1", handle, make_request(*args, artifact=str(artifact), **kw), None)
    finally:
        provider.close_session("s1", handle)


# ---------------------------------------------------------------- E-01: schema drift fails clearly

def test_e01_missing_pinned_tool_fails_startup(fake, elf_artifact):
    factory, _ = fake("missing-tool")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    with pytest.raises(ProviderError, match="schema drift.*analyze_function"):
        provider.open_session("s1", profile)


def test_e01_changed_required_parameters_fail_startup(fake, elf_artifact):
    factory, _ = fake("wrong-required")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    with pytest.raises(ProviderError, match=r"open_binary requires .*extra_guessed_param"):
        provider.open_session("s1", profile)


def test_e01_changed_enum_fails_startup(fake, elf_artifact):
    factory, _ = fake("wrong-enum")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    with pytest.raises(ProviderError, match=r"get_call_graph\.direction"):
        provider.open_session("s1", profile)


def test_e01_lost_auto_selector_fails_startup(fake, elf_artifact):
    factory, _ = fake("no-auto")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    with pytest.raises(ProviderError, match="provider_id.*auto"):
        provider.open_session("s1", profile)


def test_e01_wrong_server_version_fails_startup(fake, elf_artifact):
    factory, _ = fake("wrong-version")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    with pytest.raises(ProviderError, match="6\\.4\\.1"):
        provider.open_session("s1", profile)


def test_e01_fetch_only_uses_pinned_tools():
    used = {"open_binary", "procedure_address", "analyze_function", "read_function_instructions"}
    assert used <= set(PINNED_TOOLS)


# ---------------------------------------------------------------- E-02: typed capability matrix

def test_e02_raw_matrix_rejections():
    cases = [
        (("mips", 32, "little", "", 0x80000000), "not in the documented raw families"),
        (("ppc", 32, "big", "", 0x80000000), "not in the documented raw families"),
        (("powerpc", 32, "big", "", 0x80000000), "not in the documented raw families"),
        (("arm", 32, "little", "thumb", 0x80000000), "Thumb-only input is excluded"),
        (("arm", 32, "little", "", 0x80000000), "pin mode='arm'"),
        (("x86_64", 64, "big", "", 0x80000000), "little-endian only"),
        (("x86_64", 32, "little", "", 0x80000000), "requires (64,)-bit input"),
        (("6502", 8, "little", "", None), "entry_point"),
        (("6502", 8, "little", "", -1), "entry_point"),
    ]
    for args, why in cases:
        ok, reasons = raw_matrix_admission(*args)
        assert not ok and any(why in r for r in reasons), (args, reasons)
    ok, reasons = raw_matrix_admission("x86_64", 64, "little", "", 0x400000)
    assert ok and not reasons


def test_e02_raw_profile_typed_unsupported_with_pinned_schema_gap(tmp_path):
    raw = tmp_path / "raw.bin"
    raw.write_bytes(bytes(range(256)))
    provider = ReaProvider(host_os="darwin")
    profile = TargetProfile(kind="raw_region", processor="x86_64", bits=64, endian="little",
                            mode="", metadata={"entry_point": 0x400000})
    result = provider.capabilities(profile, raw)
    assert not result.supported
    assert any("rea_raw_route_absent" in r for r in result.reasons)
    assert any("direct Ghidra adapter" in r for r in result.reasons)
    # the caller-supplied entry_point is preserved verbatim as the offset-zero mapping, never
    # reinterpreted (P9.2/R02)
    assert result.profile["matrix"]["mapping"] == {0: 0x400000}
    assert result.profile["matrix"]["status"] == "pinned_schema_absent"


def test_e02_raw_mips_ppc_thumb_big_endian_via_capabilities(tmp_path):
    raw = tmp_path / "raw.bin"
    raw.write_bytes(bytes(range(256)))
    provider = ReaProvider(host_os="darwin")
    cases = [
        (TargetProfile(kind="raw_region", processor="mips", bits=32, endian="big", mode="",
                       metadata={"entry_point": 0}), "documented raw families"),
        (TargetProfile(kind="raw_region", processor="powerpc", bits=32, endian="big", mode="",
                       metadata={"entry_point": 0}), "documented raw families"),
        (TargetProfile(kind="raw_region", processor="arm", bits=32, endian="little", mode="thumb",
                       metadata={"entry_point": 0}), "Thumb-only"),
        (TargetProfile(kind="raw_region", processor="arm64", bits=64, endian="big", mode="",
                       metadata={"entry_point": 0}), "little-endian only"),
    ]
    for profile, why in cases:
        result = provider.capabilities(profile, raw)
        assert not result.supported and any(why in r for r in result.reasons), (profile, result.reasons)


def test_e02_headered_elf_admits_and_pins_identity(elf_artifact):
    provider = ReaProvider(host_os="darwin")
    result = provider.capabilities(make_profile(), elf_artifact)
    assert result.supported, result.reasons
    assert result.profile["provider_id"] == "ghidra"
    assert result.profile["architecture"] == "x86_64"
    assert result.profile["digest"] == hashlib.sha256(elf_artifact.read_bytes()).hexdigest()
    assert result.profile["pinned_package"]["version"] == "6.4.0"


def test_e02_dos_com_lane_explicit_only(tmp_path):
    com = tmp_path / "prog.com"
    com.write_bytes(bytes(100))
    provider = ReaProvider(host_os="darwin")
    profile = TargetProfile(kind="raw_region", processor="x86", bits=16, endian="little", mode="",
                            metadata={"rea_format": "dos-com"})
    result = provider.capabilities(profile, com)
    assert result.supported and result.profile["format"] == "dos-com"
    big = tmp_path / "big.com"
    big.write_bytes(bytes(DOS_COM_MAX_BYTES + 1))
    rejected = provider.capabilities(profile, big)
    assert not rejected.supported and any("1..65280" in r for r in rejected.reasons)


def test_e02_headered_rejections_are_typed(tmp_path):
    provider = ReaProvider(host_os="darwin")
    # big-endian ELF (same x86_64 machine, big-endian encoding)
    be = tmp_path / "be.bin"
    be.write_bytes(elf64_le(enc=2))
    r = provider.capabilities(make_profile(), be)
    assert not r.supported and any("big-endian ELF" in w for w in r.reasons)
    # PowerPC ELF
    ppc = tmp_path / "ppc.bin"
    ppc.write_bytes(elf64_le(machine=21, enc=2))
    r = provider.capabilities(make_profile(processor="powerpc", endian="big"), ppc)
    assert not r.supported and any("PowerPC" in w for w in r.reasons)
    # MIPS ELF without the pinned lane declaration: never guessed
    mips = tmp_path / "mips.bin"
    mips.write_bytes(elf32_mips())
    r = provider.capabilities(make_profile(processor="mips", bits=32), mips)
    assert not r.supported and any("exact inspected lane" in w for w in r.reasons)
    # MIPS16 ASE flag is rejected even with the lane declared
    mips16 = tmp_path / "mips16.bin"
    mips16.write_bytes(elf32_mips(flags=0x70001000 | 0x01000000))
    r = provider.capabilities(make_profile(processor="mips", bits=32,
                                           metadata={"rea_mips_lane": "elf32-exec-o32-arch32r2-standard-v3"}),
                              mips16)
    assert not r.supported and any("MIPS16" in w for w in r.reasons)
    # relocatable objects are not admitted on the pinned target model
    obj = tmp_path / "obj.o"
    obj.write_bytes(elf64_le(etype=1))
    r = provider.capabilities(make_profile(kind="object"), obj)
    assert not r.supported and any("relocatable-object" in w for w in r.reasons)
    # headerless executable: only the explicit dos-com lane exists
    bare = tmp_path / "bare.bin"
    bare.write_bytes(bytes(200))
    r = provider.capabilities(make_profile(), bare)
    assert not r.supported and any("dos-com" in w for w in r.reasons)


def test_e02_mips_lane_matrix_exact():
    ok, _ = mips_lane_admission(elf_header_info(elf32_mips()),
                                "elf32-exec-o32-arch32r2-standard-v3")
    assert ok
    for hdr, lane in [(None, "elf32-exec-o32-arch32r2-standard-v3"),
                      (elf_header_info(elf32_mips()), None),
                      (elf_header_info(elf32_mips(etype=3)), "elf32-exec-o32-arch32r2-standard-v3"),
                      (elf_header_info(elf32_mips(flags=0x70001000 | 0x20)),
                       "elf32-exec-o32-arch32r2-standard-v3")]:
        ok, reasons = mips_lane_admission(hdr, lane)
        assert not ok and reasons


# ---------------------------------------------------------------- E-03: success vs isError parsing

EVID = "ev_" + "a" * 64


def test_e03_success_envelope_parses():
    outcome = parse_tool_result({"result": {"content": [{"type": "text", "text": "{}"}],
                                            "structuredContent": {"evidence_id": EVID,
                                                                  "normalized_result": {"x": 1}}}},
                                "analyze_function")
    assert outcome.ok and outcome.value["normalized_result"] == {"x": 1} and not outcome.is_error


def test_e03_session_tool_result_unwrapped():
    outcome = parse_tool_result({"result": {"structuredContent": {"result": {"sha256": "0" * 64}}}},
                                "open_binary")
    assert outcome.ok and outcome.value == {"sha256": "0" * 64}


def test_e03_iserror_diagnostic_parsed_separately():
    text = json.dumps({"error": {"code": "boom", "message": "bad function"}})
    outcome = parse_tool_result({"result": {"content": [{"type": "text", "text": text}],
                                            "structuredContent": {"error": {"code": "boom"}},
                                            "isError": True}}, "analyze_function")
    assert not outcome.ok and outcome.is_error
    assert "bad function" in outcome.diagnostic and not outcome.recovery_ref


def test_e03_partial_observation_evidence_is_preserved():
    partial_id = "ev_" + "b" * 64
    text = json.dumps({"error": {"code": "partial",
                                 "details": {"partial_observation": {
                                     "kind": "retained-evidence", "evidence_id": partial_id}}}})
    outcome = parse_tool_result({"result": {"content": [{"type": "text", "text": text}],
                                            "structuredContent": {}, "isError": True}},
                                "analyze_function")
    assert not outcome.ok and outcome.recovery_ref == partial_id


def test_e03_oversized_recovery_reference_is_preserved():
    big_id = "ev_" + "c" * 64
    text = json.dumps({"error": {"code": "resource_constraint",
                                 "details": {"recovery": {"evidence_reference": {
                                     "kind": "retained-evidence", "evidence_id": big_id}},
                                             "response_bytes_at_least": 700000}}})
    outcome = parse_tool_result({"result": {"content": [{"type": "text", "text": text}],
                                            "structuredContent": {}, "isError": True}},
                                "analyze_function")
    assert not outcome.ok and outcome.recovery_ref == big_id and outcome.resource_hint == 700000


def test_e03_json_rpc_error_is_diagnostic():
    outcome = parse_tool_result({"error": {"code": -32601, "message": "Method not found"}}, "x")
    assert not outcome.ok and "Method not found" in outcome.diagnostic


def test_e03_batch_items_keep_partial_success():
    payload = {"result": {"structuredContent": {
        "evidence_id": EVID, "provider": {"id": "ghidra", "name": "Ghidra", "version": None},
        "operation": "batch_decompile", "subject": None,
        "normalized_result": {"items": [
            {"address": "f1", "procedure": {"status": "resolved", "address": "0x1000",
                                            "name": "f1", "name_error": None},
             "status": "ok", "pseudocode": "int f1(void) {}"},
            {"address": "f2", "procedure": {"status": "unknown",
                                            "error": {"code": "x", "message": "missing"}},
             "status": "error", "error": {"code": "x", "message": "missing"}}],
            "total": 2, "succeeded": 1, "failed": 1}}}}
    outcome = parse_tool_result(payload, "batch_decompile")
    assert outcome.ok
    items = outcome.value["normalized_result"]["items"]
    assert items[0]["status"] == "ok" and items[0]["pseudocode"]
    assert items[1]["status"] == "error"  # per-item failures stay explicit, nothing dropped


def test_e03_fetch_mixed_success_and_error(fake, elf_artifact):
    factory, _ = fake("error-f2")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    handle = provider.open_session("s1", profile)
    try:
        res = provider.fetch("s1", handle,
                             make_request(functions=("f1", "f2"), artifact=str(elf_artifact)), None)
        assert res.status == "partial"
        ev = res.functions["mod1::f1"]
        assert ev.status == "ok" and ev.views["pseudocode"]["text"]
        assert ev.refs["mod1::f1"].ref.startswith("rea:s1:ev_")
        assert any("bad function" in d for d in res.diagnostics)
        assert res.functions["mod1::f2"].status == "failed"
    finally:
        provider.close_session("s1", handle)


def test_e03_partial_evidence_keeps_retained_ref(fake, elf_artifact):
    factory, _ = fake("partial-evidence")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    handle = provider.open_session("s1", profile)
    try:
        res = provider.fetch("s1", handle,
                             make_request(artifact=str(elf_artifact)), None)
        ev = res.functions["mod1::f1"]
        assert ev.status == "partial"
        retained_id = "ev_" + hashlib.sha256(b"fake:partial-obs").hexdigest()
        assert ev.refs["mod1::f1"].ref == f"rea:s1:{retained_id}"  # the retained id is preserved
        assert "failed after recording" in ev.diagnostics[0]
    finally:
        provider.close_session("s1", handle)


# ---------------------------------------------------------------- E-04: oversize via retained views

def test_e04_oversized_dossier_retained_ref_no_silent_truncation(fake, elf_artifact):
    factory, _ = fake("oversize")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    handle = provider.open_session("s1", profile)
    try:
        res = provider.fetch("s1", handle,
                             make_request(artifact=str(elf_artifact)), None)
        ev = res.functions["mod1::f1"]
        assert ev.status == "partial"
        assert ev.coverage["pseudocode"] == "partial"
        assert ev.views == {}                       # no truncated copy presented as completeness
        ref = ev.refs["mod1::f1"]
        assert ref.size_hint == 512345              # the true retained size, not a fake length
        assert ref.ref.startswith("rea:s1:ev_")
        diag = ev.diagnostics[0]
        assert "inspect_analysis_view" in diag and "export_evidence_bundle" in diag
        assert "Nothing was truncated" in diag
        assert res.status == "partial"
    finally:
        provider.close_session("s1", handle)


# ---------------------------------------------------------------- E-05: PE targets

def test_e05_pe_admission(tmp_path):
    """PE targets go through the headless Ghidra bridge, with a warning, not an error."""
    pe = tmp_path / "app.exe"
    pe.write_bytes(pe_image(machine=0x8664))
    result = ReaProvider(host_os="darwin").capabilities(make_profile(), pe)
    assert result.supported and any("cross-platform" in w for w in result.profile["warnings"])
    arm = tmp_path / "arm.exe"
    arm.write_bytes(pe_image(machine=0xAA64))
    assert not ReaProvider(host_os="darwin").capabilities(make_profile(), arm).supported


def test_e05_macho_admission(tmp_path):
    m = tmp_path / "app.macho"
    m.write_bytes(macho_x86_64())
    provider = ReaProvider(host_os="darwin")
    result = provider.capabilities(make_profile(processor="x86_64"), m)
    assert result.supported and result.profile["architecture"] == "x86_64"
    assert result.profile["format"] == "mach-o"
    be = tmp_path / "be.macho"
    be.write_bytes(b"\xfe\xed\xfa\xce" + (0x01000007).to_bytes(4, "big") + bytes(64))
    rejected = provider.capabilities(make_profile(), be)
    assert not rejected.supported and any("little-endian x86_64/arm64" in w for w in rejected.reasons)


# ---------------------------------------------------------------- E-06: snapshots vs live queries

def test_e06_cached_observation_reused_new_snapshot_requeries(fake, elf_artifact):
    factory, state_dir = fake("default")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    handle = provider.open_session("s1", profile)
    calls = state_dir / "calls.log"
    try:
        first = provider.fetch("s1", handle,
                               make_request(artifact=str(elf_artifact), snapshot="s1"), None)
        assert first.status == "ok" and not first.reused
        count = calls.read_text().count("analyze_function\n")
        assert count == 1
        second = provider.fetch("s1", handle,
                                make_request(artifact=str(elf_artifact), snapshot="s1"), None)
        assert second.status == "ok" and second.reused          # eligible observation reused
        assert calls.read_text().count("analyze_function\n") == 1
        assert provider.refresh("s1", "mod1", "s2") == 1        # snapshot change invalidates
        third = provider.fetch("s1", handle,
                               make_request(artifact=str(elf_artifact), snapshot="s2"), None)
        assert not third.reused                                 # genuinely-new query goes back
        assert calls.read_text().count("analyze_function\n") == 2
    finally:
        provider.close_session("s1", handle)


def test_e06_snapshot_import_and_new_startup_after_close(fake, elf_artifact):
    factory, state_dir = fake("default")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    profile["snapshot_path"] = str(state_dir / "snap.json")
    handle = provider.open_session("s1", profile)
    provider.close_session("s1", handle)
    log = (state_dir / "calls.log").read_text()
    assert f"open_binary:snapshot={state_dir / 'snap.json'}" in log
    # a session closed without a snapshot: the next open re-imports; cached evidence from the
    # previous session is never served as current (no restart-free claim from a snapshot file)
    fresh_profile = dict(profile)
    fresh_profile["snapshot_path"] = None
    handle = provider.open_session("s1", fresh_profile)
    try:
        res = provider.fetch("s1", handle,
                             make_request(artifact=str(elf_artifact), snapshot="s1"), None)
        assert res.status == "ok" and not res.reused
        log = (state_dir / "calls.log").read_text()
        assert log.count("open_binary\n") == 2
        assert log.count("analyze_function\n") == 1
    finally:
        provider.close_session("s1", handle)


# ---------------------------------------------------------------- E-07: cancellation invalidates

def test_e07_cancel_invalidates_handle_and_close_reopen_recovers(fake, elf_artifact):
    factory, state_dir = fake("block-once")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    handle = provider.open_session("s1", profile)
    cancel = threading.Event()
    results = {}

    def run():
        results["r"] = provider.fetch("s1", handle,
                                      make_request(artifact=str(elf_artifact)), cancel)

    t = threading.Thread(target=run)
    t.start()
    try:
        assert wait_for(lambda: (state_dir / "blocking").exists())
        cancel.set()
        t.join(10)
        assert not t.is_alive()
        assert results["r"].status == "cancelled"
        assert handle["invalid"]  # the ephemeral database was discarded with the cancellation
        # the invalidated handle is never used half-valid: the next request must fail clearly
        with pytest.raises(ProviderError, match="invalidated"):
            provider.fetch("s1", handle, make_request(artifact=str(elf_artifact)), None)
    finally:
        if t.is_alive():
            cancel.set()
            t.join(5)
        provider.close_session("s1", handle)
        provider.close_session("s1", handle)  # idempotent
    # recovery is close/reopen: a fresh session answers normally
    fresh = provider.open_session("s2", profile)
    try:
        res = provider.fetch("s2", fresh, make_request(artifact=str(elf_artifact)), None)
        assert res.status == "ok" and res.functions["mod1::f1"].status == "ok"
    finally:
        provider.close_session("s2", fresh)


def test_e07_broker_cancellation_recovers_all_consumers(fake, elf_artifact, tmp_path):
    factory, state_dir = fake("block-once")
    provider = factory()
    broker = Broker(cache_dir=tmp_path / "cache")
    broker.register_provider(provider)
    broker.register_target("mod1", elf_artifact, make_profile(), provider="rea-ghidra")
    cancel = threading.Event()
    results = {}

    def ask(tag, function_key, ev=None):
        results[tag] = broker.get_function_evidence(function_key, ("pseudocode",),
                                                    cancel=ev, requester=tag)

    a = threading.Thread(target=ask, args=("owner", "mod1::f1", cancel))
    a.start()
    try:
        assert wait_for(lambda: (state_dir / "blocking").exists())
        # a coalesced consumer of the SAME shared request (identical identity) joins the in-flight
        c = threading.Thread(target=ask, args=("coalesced", "mod1::f1"))
        c.start()
        # a DIFFERENT function queues on the same session behind the active request
        b = threading.Thread(target=ask, args=("queued", "mod1::f2"))
        b.start()
        assert wait_for(lambda: broker.session_states() and
                        broker.session_states()[
                            next(iter(broker.session_states()))]["pending"] == 1)
        cancel.set()
        a.join(10)
        b.join(10)
        c.join(10)
        assert not a.is_alive() and not b.is_alive() and not c.is_alive()
        assert results["owner"].status == "cancelled"
        assert results["queued"].status == "retryable"      # no orphaned waiter (A-07 semantics)
        assert results["coalesced"].status == "retryable"   # shared consumers retry explicitly
        # a genuinely new request recovers through a fresh session, never a half-valid one
        recovered = broker.get_function_evidence("mod1::f1", ("pseudocode",), requester="again")
        assert recovered.status == "ok"
        assert recovered.functions["mod1::f1"].views["pseudocode"]["text"]
    finally:
        if a.is_alive():
            cancel.set()
            a.join(5)
        if b.is_alive():
            b.join(5)
        if c.is_alive():
            c.join(5)
        for key in list(broker.session_states()):
            session = broker._sessions.get(key)
            if session is not None:
                session.close()


# ---------------------------------------------------------------- E-08: identity conflicts

def test_e08_digest_change_between_admission_and_import(tmp_path, elf_artifact):
    provider = ReaProvider(host_os="darwin")  # no server needed: the digest gate fails first
    profile = admitted(provider, elf_artifact)
    elf_artifact.write_bytes(elf64_le(machine=62) + b"\x00" * 8)
    with pytest.raises(ProviderError, match="changed between admission and import"):
        provider.open_session("s1", profile)


def test_e08_digest_change_after_import_fails_fetch(fake, elf_artifact):
    factory, _ = fake("default")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    handle = provider.open_session("s1", profile)
    try:
        elf_artifact.write_bytes(elf64_le(machine=62) + b"\x01")
        with pytest.raises(ProviderError, match="changed since import"):
            provider.fetch("s1", handle, make_request(artifact=str(elf_artifact)), None)
    finally:
        provider.close_session("s1", handle)


def test_e08_provider_reported_digest_mismatch_fails_import(fake, elf_artifact):
    factory, _ = fake("identity-mismatch")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    with pytest.raises(ProviderError, match="identity conflict"):
        provider.open_session("s1", profile)


def test_e08_evidence_subject_digest_conflict_is_rejected(fake, elf_artifact):
    envelope = {"evidence_id": "ev_" + "d" * 64, "normalized_result": {},
                "provider": {"id": "ghidra", "name": "Ghidra", "version": None},
                "operation": "analyze_function",
                "subject": {"name": "f", "digest": {"sha256": "e" * 64}, "format": "elf",
                            "architecture": "x86_64", "local_path": ""}}
    with pytest.raises(ProviderError, match="subject digest"):
        validate_evidence_envelope(envelope, "analyze_function", "a" * 64, "ghidra")
    with pytest.raises(ProviderError, match="provider"):
        validate_evidence_envelope({**envelope, "subject": None}, "analyze_function", "a" * 64,
                                   "hopper")


# ---------------------------------------------------------------- E-09: unavailable vs empty

def test_e09_unavailable_stays_distinct_from_empty(fake, elf_artifact):
    factory, _ = fake("default")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    handle = provider.open_session("s1", profile)
    try:
        res = provider.fetch("s1", handle,
                             make_request(facets=("pseudocode", "pcode", "types", "calls"),
                                          artifact=str(elf_artifact)), None)
        ev = res.functions["mod1::f1"]
        assert ev.coverage["pseudocode"] == "ok"
        assert ev.coverage["pcode"] == "unavailable"          # null native_value_flow
        assert "null" in ev.views["pcode"]["reason"]
        assert ev.coverage["types"] == "unavailable"          # no pinned type-inventory tool
        assert "pinned 6.4.0" in ev.views["types"]["reason"]
        assert ev.coverage["calls"] == "empty"                # observed absence, not unknown
        assert ev.views["calls"]["items"] == []
        assert ev.coverage["instructions"] == "unknown"       # not requested
    finally:
        provider.close_session("s1", handle)


def test_e09_null_instructions_facet_is_unavailable(fake, elf_artifact):
    factory, _ = fake("instructions-null")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    handle = provider.open_session("s1", profile)
    try:
        res = provider.fetch("s1", handle,
                             make_request(facets=("pseudocode", "instructions"),
                                          artifact=str(elf_artifact)), None)
        ev = res.functions["mod1::f1"]
        assert ev.coverage["instructions"] == "unavailable"
        assert "null" in ev.views["instructions"]["reason"]
    finally:
        provider.close_session("s1", handle)


def test_e09_missing_function_is_failed_not_empty(fake, elf_artifact):
    factory, _ = fake("missing-function")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    handle = provider.open_session("s1", profile)
    try:
        res = provider.fetch("s1", handle, make_request(artifact=str(elf_artifact)), None)
        ev = res.functions["mod1::f1"]
        assert ev.status == "failed"
        assert ev.coverage["pseudocode"] == "failed"
        assert any("no procedure" in d for d in ev.diagnostics)
    finally:
        provider.close_session("s1", handle)


# ---------------------------------------------------------------- P9.7 provider failure handling

def test_p97_killed_sidecar_mid_request_fails_session_and_leaves_no_child(fake, elf_artifact):
    factory, state_dir = fake("kill-mid-request")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    handle = provider.open_session("s1", profile)
    proc = handle["client"].proc
    try:
        with pytest.raises(rea.ReaProviderFailure, match="exited|closed stdout|transport failure"):
            provider.fetch("s1", handle, make_request(artifact=str(elf_artifact)), None)
        assert "analyze_function" in (state_dir / "calls.log").read_text()  # it died mid-request
        assert handle["cache"] == {}                # nothing from the dead request is kept as complete
        # the dead session is never used half-valid: the next request fails clearly too
        with pytest.raises(rea.ReaProviderFailure):
            provider.fetch("s1", handle, make_request(artifact=str(elf_artifact)), None)
        assert handle["cache"] == {}
    finally:
        provider.close_session("s1", handle)
        provider.close_session("s1", handle)  # idempotent
    assert proc.poll() is not None              # no live child process remains
    assert handle["client"].proc is None


def test_p97_malformed_mcp_output_fails_session_and_leaves_no_child(fake, elf_artifact):
    factory, _ = fake("malformed-output")
    provider = factory()
    profile = admitted(provider, elf_artifact)
    handle = provider.open_session("s1", profile)
    proc = handle["client"].proc
    try:
        with pytest.raises(rea.ReaProviderFailure, match="malformed JSON"):
            provider.fetch("s1", handle, make_request(artifact=str(elf_artifact)), None)
        assert handle["cache"] == {}                # garbage output never becomes cached evidence
    finally:
        provider.close_session("s1", handle)
    assert proc.poll() is not None                  # the still-running sidecar was terminated
    # a well-formed JSON-RPC reply whose text part is not JSON is a failed outcome, never success
    outcome = parse_tool_result({"result": {"content": [{"type": "text", "text": "not json{"}]}},
                                "analyze_function")
    assert not outcome.ok and outcome.is_error and "malformed" in outcome.diagnostic
    assert outcome.value is None


def test_p97_cancelled_export_is_a_failure_and_leaves_no_child(fake, tmp_path):
    factory, state_dir = fake("block-export")
    provider = factory()
    client = ReaMCPClient(provider.command, cwd=provider.cwd, env=provider.env,
                          start_timeout=30.0)
    client.start()
    proc = client.proc
    export_path = tmp_path / "bundle.json"
    cancel = threading.Event()
    results = {}

    def run():
        results["outcome"] = client.call("export_evidence_bundle", {"path": str(export_path)},
                                         cancel=cancel, deadline=time.monotonic() + 30.0)

    t = None
    try:
        client.initialize()
        t = threading.Thread(target=run)
        t.start()
        assert wait_for(lambda: (state_dir / "blocking").exists())
        cancel.set()
        t.join(10)
        assert not t.is_alive()
        outcome = results["outcome"]
        assert not outcome.ok and outcome.is_error
        assert "cancelled" in outcome.diagnostic
        assert outcome.value is None and not outcome.recovery_ref  # no partial export reported
        assert (state_dir / "cancel_ack").exists()                 # the server saw the cancel
        assert not export_path.exists()                            # no half-written bundle
    finally:
        if t is not None and t.is_alive():
            cancel.set()
            t.join(5)
        client.close()
        client.close()  # idempotent
    assert proc.poll() is not None


def test_p97_spawn_failure_raises_provider_failure_and_leaves_no_session(tmp_path, elf_artifact):
    missing = tmp_path / "no-such-rea-binary"
    provider = ReaProvider(command=[str(missing)], cwd=tmp_path,
                           env={"PATH": os.environ.get("PATH", ""), "LANG": "C"}, host_os="darwin")
    profile = admitted(provider, elf_artifact)
    with pytest.raises(rea.ReaProviderFailure):
        provider.open_session("s1", profile)
    assert provider._sessions == {}                 # no half-open session is registered


# ---------------------------------------------------------------- engine correlation (A-04)

class _SameEngineProvider(Provider):
    """A minimal provider that reports a fixed engine; stands in for one route over that engine."""

    def __init__(self, name, engine):
        self.name = name
        self.engine = engine

    def capabilities(self, target_profile, artifact=None):
        return CapabilityResult(True, self.name, (), profile={"kind": "executable", "auto": False})

    def open_session(self, session_key, profile):
        return {"session": session_key}

    def close_session(self, session_key, handle):
        pass

    def fetch(self, session_key, handle, request, cancel):
        functions = {}
        for name in request.functions:
            key = f"{request.inputs['module']}::{name}"
            functions[key] = FunctionEvidence(
                function_key=key, provider=self.name, engine=self.engine, correlation_group="",
                snapshot=request.snapshot, status="ok", coverage={"pseudocode": "ok"},
                views={"pseudocode": {"status": "ok", "text": f"int {name}(void) {{}}"}}, entry=0x1000)
        return ProviderResult(status="ok", functions=functions, wall_seconds=0.01)

    def refresh(self, session_key, scope, new_snapshot):
        return 0

    def flush(self, session_key, *, view_invalidated, reason):
        pass


def test_rea_and_direct_routes_share_one_correlation_group(tmp_path, elf_artifact):
    engine = ReaProvider(host_os="darwin").engine
    broker = Broker()
    direct = _SameEngineProvider("direct-ghidra", engine)
    via_rea = _SameEngineProvider("rea-ghidra", engine)
    other = _SameEngineProvider("older-ghidra", "ghidra-0.0.1")
    for p in (direct, via_rea, other):
        broker.register_provider(p)
    broker.register_target("mod1", elf_artifact, make_profile(), provider="direct-ghidra")
    try:
        b_direct = broker.get_function_evidence("mod1::f1", ("pseudocode",), provider="direct-ghidra")
        b_rea = broker.get_function_evidence("mod1::f1", ("pseudocode",), provider="rea-ghidra")
        b_other = broker.get_function_evidence("mod1::f1", ("pseudocode",), provider="older-ghidra")
        assert b_direct.status == "ok" and b_rea.status == "ok"
        assert b_direct.provider == "direct-ghidra" and b_rea.provider == "rea-ghidra"
        assert b_direct.correlation_group and b_direct.correlation_group == b_rea.correlation_group
        assert independent_sources(b_direct.correlation_group, b_rea.correlation_group) is False
        # a different engine IS an independent source
        assert independent_sources(b_direct.correlation_group, b_other.correlation_group) is True
        # new evidence is counted once per group: three requests, two underlying evidence identities
        assert broker.metrics_summary()["new_evidence_groups"] == 2
    finally:
        for key in list(broker.session_states()):
            session = broker._sessions.get(key)
            if session is not None:
                session.close()


def test_engine_correlation_matches_direct_ghidra(tmp_path):
    direct = DirectGhidraProvider(cache_dir=tmp_path / "ghidra-cache")
    provider = ReaProvider(host_os="darwin")
    assert provider.engine == direct.engine
    assert provider.engine.startswith("ghidra-")


# ---------------------------------------------------------------- P9.3 sidecar hygiene

def test_allowed_env_is_minimal(monkeypatch):
    monkeypatch.setenv("REA_SECRET_TOKEN", "do-not-leak")
    env = rea.allowed_env()
    assert "REA_SECRET_TOKEN" not in env
    assert set(env) <= set(rea.REA_ENV_ALLOWLIST)


def test_pinned_artifact_metadata_recorded():
    assert rea.REA_PACKAGE_VERSION == "6.4.0"
    assert rea.REA_MCP_NAME == "io.github.morluto/rea"
    assert rea.REA_DISCOVERY["tool_count"] == 170
    assert rea.REA_DISCOVERY["server"] == {"name": "rea", "version": "6.4.0"}
    doc = rea.__doc__ or ""
    for gap in ("GAP-1", "GAP-2", "GAP-3"):
        assert gap in doc  # the gap list a human must approve stays visible in module docs


# ---------------------------------------------------------------- real lane (gated, read-only)

def test_real_lane_schema_discovery(tmp_path):
    """The real install answers initialize + tools/list with the pinned catalog; the pin table
    itself is validated against the live schema. Read-only: isolated HOME/TMPDIR, no artifact
    opened, no user workspace touched. Skipped when the install is absent (GAP-6 evidence)."""
    if not rea.available():
        pytest.skip("rea-agents 6.4.0 is not installed on this host (no node + scripts/rea.mjs)")
    command, root = rea.default_command()
    env = {"HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path / "tmp"),
           "PATH": os.environ.get("PATH", ""), "LANG": "C"}
    client = ReaMCPClient(command, cwd=root, env=env, start_timeout=120.0)
    client.start()
    try:
        info = client.initialize()
        assert info["serverInfo"] == {"name": "rea", "version": "6.4.0"}
        catalog = client.list_tools()  # raises ReaSchemaDrift when the live catalog drifts
        assert len(catalog) >= 170
        assert set(PINNED_TOOLS) <= set(catalog)
    finally:
        client.close()


def test_real_lane_open_binary_and_analyze_function(tmp_path):
    """Workstream C real lane: the actual REA sidecar imports a host-built Mach-O executable through
    its Ghidra bridge and returns real dossiers (open_binary + analyze_function +
    read_function_instructions). Read-only: the artifact is only ever read. Skips only when
    rea-agents/node are absent or when neither the workspace Java (~/Decomp/java) nor a system Java
    exists (REA's Ghidra bridge requires a full JDK 21+ — a runtime-only JRE is explicitly rejected
    by its doctor check)."""
    import shutil
    import subprocess

    if not rea.available():
        pytest.skip("rea-agents 6.4.0 is not installed on this host (no node + scripts/rea.mjs)")
    from decompnet.forks import ghidra

    java = ghidra.java_home()
    if java is None or ghidra.java_version() < 21 or ghidra.ghidra_home() is None:
        pytest.skip("the REA Ghidra bridge needs a Java 21+ runtime (DECOMP_JAVA_HOME, ~/Decomp/java "
                    "or PATH) and a Ghidra install: none found on this host")
    cc = shutil.which("clang")
    if cc is None:
        pytest.skip("clang is needed to build the Mach-O fixture")
    src = tmp_path / "fixture.c"
    src.write_text('#include <stdio.h>\nstatic int twice(int x) { return x * 2; }\n'
                   'int main(void) { printf("%d\\n", twice(21)); return 0; }\n')
    exe = tmp_path / "fixture"
    subprocess.run([cc, str(src), "-o", str(exe)], check=True, capture_output=True)
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    provider = rea.ReaProvider()
    side = provider.open_session("s1", {"artifact": str(exe), "digest": digest,
                                        "rea_home": str(tmp_path / "home"),
                                        "rea_tmp": str(tmp_path / "tmp")})
    try:
        request = ProviderRequest(request_id="r1", kind="function", function_key="x::_twice",
                                  functions=("_twice", "_main"), facets=("pseudocode", "instructions"),
                                  snapshot="current", budget=Budget(wall_seconds=600), limits={},
                                  profile={}, inputs={"module": "x"}, requester="real-lane")
        result = provider.fetch("s1", side, request, None)
        assert result.status == "ok", result.diagnostics
        twice = result.functions.get("x::_twice")
        assert twice is not None and twice.status == "ok" and twice.entry is not None
        pseudocode = (twice.views.get("pseudocode") or {}).get("text", "")
        assert "<<" in pseudocode  # x * 2 decompiles to a shift through the real Ghidra bridge
        instructions = (twice.views.get("instructions") or {}).get("items") or []
        assert instructions  # real ARM64 instructions with addresses
        assert result.functions["x::_main"].status == "ok"
    finally:
        provider.close_session("s1", side)
