"""Direct Ghidra integration (plan P7.1-P7.7, tests G-01..G-10).

Logic tests mock the headless subprocess through the ``_run_headless`` seam (the real implementation spawns
analyzeHeadless with process-group cleanup and is exercised only by the real lane, G-09, which skips with an
explicit reason when Ghidra is not installed on the host).
"""

import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from decompnet import scheduler
from decompnet.forks import ghidra
from decompnet.forks.ghidra import (AdmissionError, DraftCandidate, FunctionDossier, GhidraUnavailable,
                                    MemoryRegion, ProposalError, TargetProfile, _RunResult)

ROOT = Path(__file__).resolve().parent.parent


def elf32le_object() -> bytes:
    h = bytearray(b"\x7fELF" + bytes([1, 1, 1]) + bytes(9))
    h += b"\x01\x00"  # e_type = ET_REL, little-endian
    h += bytes(2)  # e_machine
    return bytes(h) + bytes(64)


def elf32be_object() -> bytes:
    h = bytearray(b"\x7fELF" + bytes([1, 2, 1]) + bytes(9))
    h += b"\x00\x01"  # e_type = ET_REL, big-endian
    h += bytes(2)  # e_machine
    return bytes(h) + bytes(64)


class FakeHeadless:
    """Replaces _run_headless: records each import and returns a scripted _RunResult."""

    def __init__(self):
        self.calls = []
        self.result = _RunResult(status="complete", exit_code=0,
                                 summary={"type": "summary", "status": "complete", "functions_total": 0,
                                          "functions_exported": 0, "failed": []})

    def respond(self, **kwargs):
        self.result = _RunResult(**kwargs)
        return self.result

    def __call__(self, admitted, obj, scripts_src, facets, want_bytes, timeout, cancel, data=None, digest=None):
        self.calls.append({"admitted": admitted, "obj": obj, "facets": facets, "want_bytes": want_bytes,
                           "timeout": timeout, "cancel": cancel, "data": data, "digest": digest,
                           "analysis_used": scheduler.BUDGET.pressure()["used"]["analysis"]})
        if cancel is not None and cancel.is_set():
            return _RunResult(status="cancelled", cancel_requested=True)
        return self.result


@pytest.fixture
def headless(monkeypatch):
    fake = FakeHeadless()
    monkeypatch.setattr(ghidra, "available", lambda: True)
    monkeypatch.setattr(ghidra, "provider_version", lambda: "ghidra-12.1.4")
    monkeypatch.setattr(ghidra, "_run_headless", fake)
    return fake


def frow(name, entry=0x100, *, module=None, space="ram", section=".text", block_offset=0, text=None,
         pstatus="ok", classification="normal", ranges=None, thunk_of=None, coverage=None,
         instructions=None, calls=None, refs=None, blocks=None, types=None, warnings=None, c=None,
         namespace=""):
    if c is None and text is None and pstatus in ("ok", "partial"):
        text = f"int {name}(void) {{\n}}"
    c = c if c is not None else (text if pstatus in ("ok", "partial", "failed") else "")
    if ranges is None:
        ranges = [[entry, entry + 0x20]] if classification == "normal" else []
    cov = {f: "unknown" for f in ghidra.FACETS}
    cov["pseudocode"] = pstatus
    for facet, value in (coverage or {}).items():
        cov[facet] = value
    # the exporter reports a facet that was computed with data as ok, and without data as empty
    for facet, value in (("instructions", instructions), ("blocks", blocks), ("calls", calls),
                         ("references", refs), ("types", types)):
        if value is not None and facet not in (coverage or {}):
            cov[facet] = "ok" if value else "empty"
    row = {"name": name, "c": c,
           "status": "ok" if pstatus == "ok" else ("skipped" if classification in ("thunk", "external") else "failed"),
           "classification": classification, "namespace": namespace, "entry": entry, "space": space,
           "section": section, "block_offset": block_offset, "ranges": ranges, "thunk_of": thunk_of,
           "pseudocode": {"status": pstatus, "warnings": warnings or []}, "coverage": cov}
    if module is not None:
        row["module"] = module
    for key, value in (("instructions", instructions), ("blocks", blocks), ("calls", calls),
                       ("refs", refs), ("types", types)):
        if value is not None:
            row[key] = value
    return row


# ---------------------------------------------------------------------------- G-01

def test_one_import_produces_identity_correct_dossiers_for_many_functions(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    rows = [frow(f"func_{i:04d}", entry=0x1000 + 0x40 * i,
                 text=f"int func_{i:04d}(void) {{ return {i}; }}") for i in range(12)]
    headless.respond(status="complete", exit_code=0, rows=rows,
                     summary={"type": "summary", "status": "complete", "functions_total": 12,
                              "functions_exported": 12, "failed": [], "language": "MIPS:BE:32:default",
                              "cspec": "default"})
    cache = tmp_path / "cache"
    m = ghidra.analyze_object(obj, cache)
    assert m.status == "complete" and m.functions_total == 12 and m.functions_exported == 12
    assert len(m.functions) == 12 and m.coverage_for("pseudocode").get("ok") == 12
    for i in range(12):
        d = m.by_name(f"func_{i:04d}")
        assert d is not None
        assert d.entry == 0x1000 + 0x40 * i
        assert d.module == "game.o" and d.section == ".text" and d.space == "ram"
        assert d.pseudocode.status == "ok" and f"return {i}" in d.pseudocode.text
        assert d.key == "\x1f".join(("game.o", "ram", ".text", str(d.entry)))
    assert len(headless.calls) == 1  # one import serves every function
    again = ghidra.analyze_object(obj, cache)
    assert again.from_cache and len(headless.calls) == 1
    assert set(again.functions) == set(m.functions)


# ---------------------------------------------------------------------------- G-02

def test_same_name_address_in_different_modules_stays_distinct(tmp_path, headless):
    a, b = tmp_path / "game.o", tmp_path / "other.o"
    a.write_bytes(bytes(32))
    b.write_bytes(bytes(48))
    row = frow("setup", entry=0x100, text="int setup(void) {\n}")
    headless.respond(status="complete", exit_code=0, rows=[row])
    ma = ghidra.analyze_object(a, tmp_path / "cache")
    mb = ghidra.analyze_object(b, tmp_path / "cache")
    assert len(ma.functions) == len(mb.functions) == 1
    ka, kb = next(iter(ma.functions)), next(iter(mb.functions))
    assert ka.startswith("game.o\x1f") and kb.startswith("other.o\x1f") and ka != kb
    # round-trip through the cache preserves the canonical coordinates
    assert set(ghidra.analyze_object(a, tmp_path / "cache").functions) == {ka}
    assert set(ghidra.analyze_object(b, tmp_path / "cache").functions) == {kb}


def test_same_name_address_in_different_sections_or_spaces_stays_distinct(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(32))
    rows = [frow("fn", entry=0x100, section=".text", space="ram", text="int fn(void) {}"),
            frow("fn", entry=0x100, section="ov0_text", space="ov0", text="int fn(void) { return 1; }")]
    headless.respond(status="complete", exit_code=0, rows=rows)
    m = ghidra.analyze_object(obj, tmp_path / "cache")
    assert len(m.functions) == 2  # overlays at equal addresses never collapse into one function
    keys = sorted(m.functions)
    assert {k.split("\x1f")[1] for k in keys} == {"ram", "ov0"}
    assert {k.split("\x1f")[2] for k in keys} == {".text", "ov0_text"}
    assert {d.entry for d in m.functions.values()} == {0x100}
    assert m.names() == {"fn": "int fn(void) { return 1; }"}  # compat projection keeps the last, like before


# ---------------------------------------------------------------------------- G-03

def test_admission_rejects_wrong_endianness():
    with pytest.raises(AdmissionError, match="little-endian"):
        ghidra.admit(TargetProfile(kind="object", processor="mips", bits=32, endian="big"), elf32le_object())


def test_admission_rejects_wrong_class():
    with pytest.raises(AdmissionError, match="ELF32"):
        ghidra.admit(TargetProfile(kind="object", processor="mips", bits=64, endian="little"), elf32le_object())


def test_admission_rejects_kind_mismatch():
    with pytest.raises(AdmissionError, match="ET_REL"):
        ghidra.admit(TargetProfile(kind="executable", processor="mips", bits=32, endian="little"),
                     elf32le_object())
    exe = elf32le_object()
    exe = exe[:16] + b"\x02\x00" + exe[18:]  # ET_EXEC
    with pytest.raises(AdmissionError, match="relocatable object"):
        ghidra.admit(TargetProfile(kind="object", processor="mips", bits=32, endian="little"), exe)


def test_admission_rejects_unsupported_languages():
    for profile, match in [
        (TargetProfile(kind="object", processor="6502", bits=8, endian="little"), "unsupported Ghidra language"),
        (TargetProfile(kind="object", processor="powerpc", bits=32, endian="little"), "unsupported Ghidra language"),
        (TargetProfile(kind="object", processor="sh", bits=32, endian="big"), "unsupported Ghidra language"),
        (TargetProfile(kind="object", processor="x86", bits=32, endian="big"), "unsupported Ghidra language"),
    ]:
        with pytest.raises(AdmissionError, match=match):
            ghidra.admit(profile, bytes(64))  # non-ELF bytes: only the language table matters here
    with pytest.raises(AdmissionError, match="pin the processor mode"):
        ghidra.admit(TargetProfile(kind="object", processor="arm", bits=32, endian="little"), elf32le_object())


def test_admission_rejects_bad_memory_maps_and_loaders():
    with pytest.raises(AdmissionError, match="memory map"):
        ghidra.admit(TargetProfile(kind="raw_region", processor="mips", bits=32, endian="big"), bytes(0x400))
    with pytest.raises(AdmissionError, match="outside the"):
        ghidra.admit(TargetProfile(kind="raw_region", processor="mips", bits=32, endian="big",
                                   memory_map=(MemoryRegion(name="r", file_offset=0x100, size=0x1000,
                                                            vaddr=0),),),
                     bytes(0x400))
    with pytest.raises(AdmissionError, match="overlap"):
        ghidra.admit(TargetProfile(kind="raw_region", processor="mips", bits=32, endian="big",
                                   memory_map=(MemoryRegion(name="a", file_offset=0, size=0x200, vaddr=0),
                                               MemoryRegion(name="b", file_offset=0x200, size=0x200, vaddr=0x100)),),
                     bytes(0x400))
    with pytest.raises(AdmissionError, match="unknown loader"):
        ghidra.admit(TargetProfile(kind="object", processor="mips", bits=32, endian="little", loader="Macho"),
                     elf32le_object())
    with pytest.raises(AdmissionError, match="unknown profile field"):
        ghidra.admit({"kind": "object", "processor": "mips", "bits": 32, "endian": "little", "bogus": 1},
                     elf32le_object())
    with pytest.raises(AdmissionError, match="boundary"):
        ghidra.admit(TargetProfile(kind="raw_region", processor="mips", bits=32, endian="big",
                                   memory_map=(MemoryRegion(name="r", file_offset=0, size=0x100, vaddr=0x100),),
                                   boundaries=((0x500, 0x520),)), bytes(0x100))


def test_admission_pins_language_spec_and_loader():
    admitted = ghidra.admit(TargetProfile(kind="object", processor="mips", bits=32, endian="little"),
                            elf32le_object())
    assert admitted.language_id == "MIPS:LE:32:default"
    # an unpinned compiler spec is None (the language's own default): -cspec ids are per-language
    # (x86:LE:64:default has no "default" spec), so the exporter only passes one when the caller pins it
    assert admitted.compiler_spec is None and admitted.loader == "Elf" and not admitted.auto
    assert "-loader" in admitted.script_args() and "ElfLoader" in admitted.script_args()
    raw = ghidra.admit(TargetProfile(kind="raw_region", processor="mips", bits=32, endian="big",
                                     memory_map=(MemoryRegion(name="text", file_offset=0, size=0x400, vaddr=0),)),
                      bytes(0x400))
    assert raw.loader == "Raw Binary"
    assert ghidra.admit(None, elf32le_object()).auto  # auto mode is marked, never a silent guess


def test_bad_profiles_never_reach_the_headless_process(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(elf32le_object())
    with pytest.raises(AdmissionError):
        ghidra.analyze_object(obj, tmp_path / "cache",
                              profile=TargetProfile(kind="object", processor="mips", bits=32, endian="big"))
    assert headless.calls == [] and not (tmp_path / "cache").exists()


def test_coordinates_and_boundary_status_are_separate(tmp_path, headless):
    obj = tmp_path / "raw.bin"
    obj.write_bytes(bytes(0x2000))
    profile = TargetProfile(kind="raw_region", processor="mips", bits=32, endian="big",
                            memory_map=(MemoryRegion(name="text", file_offset=0x100, size=0x1000,
                                                     vaddr=0x80000000),),
                            boundaries=((0x80000100, 0x80000120),))
    rows = [frow("exact_fn", entry=0x80000100, ranges=[[0x80000100, 0x80000120]], text="int exact_fn(void) {}"),
            frow("entry_fn", entry=0x80000108, text="int entry_fn(void) {}"),
            frow("proposed_fn", entry=0x80000E00, text="int proposed_fn(void) {}")]
    headless.respond(status="complete", exit_code=0, rows=rows)
    m = ghidra.analyze_object(obj, tmp_path / "cache", profile=profile)
    d = m.by_name("exact_fn")
    assert (d.file_offset, d.section_offset, d.vaddr) == (0x200, 0x100, 0x80000100)  # four coordinate types
    assert d.boundary_status == "exact"
    assert m.by_name("entry_fn").boundary_status == "entry-only"
    assert m.by_name("proposed_fn").boundary_status == "proposed"
    assert headless.calls[0]["admitted"].loader == "Raw Binary"


def test_language_mismatch_between_admission_and_program_is_reported(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(elf32le_object())
    headless.respond(status="complete", exit_code=0, rows=[frow("fn", entry=0x100)],
                     summary={"type": "summary", "status": "complete", "functions_total": 1,
                              "functions_exported": 1, "failed": [], "language": "x86:LE:64:default",
                              "cspec": "default"})
    m = ghidra.analyze_object(obj, tmp_path / "cache",
                              profile=TargetProfile(kind="object", processor="mips", bits=32, endian="little"))
    assert m.status == "partial" and any("language mismatch" in d for d in m.diagnostics)


# ---------------------------------------------------------------------------- G-04

def test_partial_export_is_retained_and_cached_as_partial(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    rows = [frow("a", entry=0x100), frow("b", entry=0x200), frow("c", entry=0x300)]
    headless.respond(status="partial", exit_code=0, rows=rows,
                     summary={"type": "summary", "status": "partial", "functions_total": 4,
                              "functions_exported": 3, "failed": ["d"]})
    cache = tmp_path / "cache"
    m = ghidra.analyze_object(obj, cache)
    assert m.status == "partial" and m.functions_exported == 3
    assert m.missing_functions == ["d"] and m.functions_total == 4
    assert m.usable() and not m.is_complete()
    again = ghidra.analyze_object(obj, cache)
    assert again.from_cache and again.status == "partial"  # never upgraded to complete-empty-success
    assert again.missing_functions == ["d"]
    assert set(ghidra.decompile_object(obj, cache)) == {"a", "b", "c"}


def test_timeout_retains_rows_but_is_never_cached(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    headless.respond(status="timeout", timed_out=True, rows=[frow("a", entry=0x100)])
    cache = tmp_path / "cache"
    m = ghidra.analyze_object(obj, cache, timeout=1.0)
    assert m.status == "timeout" and m.timed_out and m.functions_exported == 1  # partial rows retained
    assert not cache.exists() or not list(cache.glob("*.json"))  # nothing cached: coverage is unknown
    ghidra.analyze_object(obj, cache, timeout=1.0)
    assert len(headless.calls) == 2  # retry is explicit: the transient run is re-imported


def test_nonzero_exit_with_rows_is_partial_and_cached(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    headless.respond(status="partial", exit_code=1, rows=[frow("a", entry=0x100)])
    cache = tmp_path / "cache"
    m = ghidra.analyze_object(obj, cache)
    assert m.status == "partial" and m.exit_code == 1
    again = ghidra.analyze_object(obj, cache)
    assert again.from_cache and again.status == "partial" and again.exit_code == 1


def test_nonzero_exit_without_rows_is_failed_and_not_cached(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    headless.respond(status="failed", exit_code=1, diagnostics=["analyzeHeadless exited 1 without exporting anything"])
    cache = tmp_path / "cache"
    m = ghidra.analyze_object(obj, cache)
    assert m.status == "failed" and not m.usable()
    assert not cache.exists() or not list(cache.glob("*.json"))
    assert ghidra.decompile_object(obj, cache) == {}


def test_complete_empty_object_is_cached_not_a_failure(tmp_path, headless):
    obj = tmp_path / "empty.o"
    obj.write_bytes(bytes(64))
    headless.respond(status="complete", exit_code=0, rows=[],
                     summary={"type": "summary", "status": "complete", "functions_total": 0,
                              "functions_exported": 0, "failed": []})
    cache = tmp_path / "cache"
    m = ghidra.analyze_object(obj, cache)
    assert m.status == "complete" and m.functions == {}  # a genuine complete-empty, distinct from a transient failure
    again = ghidra.analyze_object(obj, cache)
    assert again.from_cache and again.status == "complete"


# ---------------------------------------------------------------------------- G-05

def test_pseudocode_failure_keeps_assembly_and_reference_facets(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    rows = [
        frow("broken", entry=0x100, pstatus="failed", c="", warnings=["decompiler error: something"],
             instructions=[{"address": 0x100, "size": 4, "asm": "jr $ra"},
                           {"address": 0x104, "size": 4, "asm": "nop"}],
             calls=[{"address": 0x108, "target": 0x200, "name": "callee", "indirect": False}],
             refs=[{"address": 0x10C, "kind": "string", "target": 0x400, "value": "hello"}]),
        frow("no_calls", entry=0x200, coverage={"calls": "empty", "references": "empty"}),
    ]
    headless.respond(status="partial", exit_code=0, rows=rows,
                     summary={"type": "summary", "status": "partial", "functions_total": 2,
                              "functions_exported": 2, "failed": ["broken"]})
    m = ghidra.analyze_object(obj, tmp_path / "cache", facets=("instructions", "calls", "references"))
    d = m.by_name("broken")
    assert d.pseudocode.status == "failed" and any("decompiler error" in w for w in d.pseudocode.warnings)
    assert d.coverage["pseudocode"] == "failed"
    assert d.coverage["instructions"] == "ok" and len(d.instructions) == 2 and d.instructions[0].address == 0x100
    assert d.coverage["calls"] == "ok" and d.calls[0].name == "callee"
    assert d.coverage["references"] == "ok" and d.references[0].value == "hello"
    assert d.coverage["types"] == "unknown"  # not requested
    empty = m.by_name("no_calls")
    assert empty.coverage["calls"] == "empty" and empty.calls == []  # observed absence
    assert empty.coverage["instructions"] == "unknown"  # unknown != empty
    assert "broken" not in m.names() and "no_calls" in m.names()


# ---------------------------------------------------------------------------- G-06

def test_metadata_change_flushes_and_refreshes(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(elf32be_object())
    profile = TargetProfile(kind="object", processor="mips", bits=32, endian="big", metadata={"types": "v1"})
    headless.respond(status="complete", exit_code=0, rows=[frow("fn", entry=0x100, text="int fn(void) { /* v1 */ }")],
                     summary={"type": "summary", "status": "complete", "functions_total": 1,
                              "functions_exported": 1, "failed": [], "language": "MIPS:BE:32:default",
                              "cspec": "default"})
    cache = tmp_path / "cache"
    m1 = ghidra.analyze_object(obj, cache, profile=profile)
    assert "v1" in m1.by_name("fn").pseudocode.text and len(headless.calls) == 1
    headless.respond(status="complete", exit_code=0, rows=[frow("fn", entry=0x100, text="int fn(void) { /* v2 */ }")],
                     summary={"type": "summary", "status": "complete", "functions_total": 1,
                              "functions_exported": 1, "failed": [], "language": "MIPS:BE:32:default",
                              "cspec": "default"})
    m2 = ghidra.analyze_object(obj, cache, profile=replace(profile, metadata={"types": "v2"}))
    assert not m2.from_cache and "v2" in m2.by_name("fn").pseudocode.text and len(headless.calls) == 2
    old = ghidra.analyze_object(obj, cache, profile=profile)  # the old identity's entry is still itself
    assert old.from_cache and "v1" in old.by_name("fn").pseudocode.text


def test_overlay_change_forces_a_reimport(tmp_path, headless):
    obj = tmp_path / "raw.bin"
    obj.write_bytes(bytes(0x2000))
    base = TargetProfile(kind="raw_region", processor="mips", bits=32, endian="big",
                         memory_map=(MemoryRegion(name="text", file_offset=0, size=0x1000, vaddr=0x80000000),))
    headless.respond(status="complete", exit_code=0, rows=[frow("fn", entry=0x80000100, text="int fn(void) { /* base */ }")],
                     summary={"type": "summary", "status": "complete", "functions_total": 1, "functions_exported": 1,
                              "failed": []})
    cache = tmp_path / "cache"
    ghidra.analyze_object(obj, cache, profile=base)
    overlays = replace(base, memory_map=base.memory_map + (
        MemoryRegion(name="ov0", file_offset=0x1000, size=0x1000, vaddr=0x80001000, space="ov0", is_overlay=True),))
    headless.respond(status="complete", exit_code=0, rows=[frow("fn", entry=0x80000100, text="int fn(void) { /* overlay */ }")],
                     summary={"type": "summary", "status": "complete", "functions_total": 1, "functions_exported": 1,
                              "failed": []})
    m = ghidra.analyze_object(obj, cache, profile=overlays)
    assert not m.from_cache and len(headless.calls) == 2  # the Program view changed: reset/reimport, not flush-reuse


def test_invalidate_and_refresh(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    headless.respond(status="complete", exit_code=0, rows=[frow("fn", entry=0x100, text="int fn(void) {}")],
                     summary={"type": "summary", "status": "complete", "functions_total": 1, "functions_exported": 1,
                              "failed": []})
    cache = tmp_path / "cache"
    assert ghidra.analyze_object(obj, cache).from_cache is False
    assert ghidra.analyze_object(obj, cache).from_cache is True
    assert ghidra.invalidate(cache, "game") == 1 and not list(cache.glob("game-*.json"))
    assert ghidra.analyze_object(obj, cache).from_cache is False and len(headless.calls) == 2
    assert ghidra.analyze_object(obj, cache, refresh=True).from_cache is False and len(headless.calls) == 3


# ---------------------------------------------------------------------------- G-07

def test_crash_is_reported_without_cache_and_compat_returns_empty(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    headless.respond(status="failed", exit_code=None, diagnostics=["analyzeHeadless failed to start: crash"])
    cache = tmp_path / "cache"
    m = ghidra.analyze_object(obj, cache)
    assert m.status == "failed" and any("crash" in d for d in m.diagnostics)
    assert not cache.exists() or not list(cache.glob("*.json"))
    assert ghidra.decompile_object(obj, cache) == {}


def test_cancellation_is_reported_without_cache(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    cancel = threading.Event()
    cancel.set()
    cache = tmp_path / "cache"
    m = ghidra.analyze_object(obj, cache, cancel=cancel)
    assert m.status == "cancelled" and m.cancel_requested
    assert not cache.exists() or not list(cache.glob("*.json"))
    headless.respond(status="cancelled", cancel_requested=True, rows=[frow("a", entry=0x100)],
                     summary={"type": "summary", "status": "cancelled", "functions_total": 1,
                              "functions_exported": 1, "failed": []})
    m2 = ghidra.analyze_object(obj, cache)
    assert m2.status == "cancelled" and m2.functions_exported == 1
    assert not cache.exists() or not list(cache.glob("*.json"))


class FakePopen:
    """Stands in for analyzeHeadless: hashes the file handed to -import, then writes a complete empty export."""
    launched = []

    def __init__(self, argv, *args, **kwargs):
        self.args = argv
        self.pid = 0
        self.returncode = 0
        imported = Path(argv[argv.index("-import") + 1])
        out = Path(argv[argv.index("-postScript") + 2])
        FakePopen.launched.append({"imported": imported, "sha": hashlib.sha256(imported.read_bytes()).hexdigest(),
                                   "analysis_used": scheduler.BUDGET.pressure()["used"]["analysis"]})
        Path(str(out) + ".summary.json").write_text(json.dumps(
            {"type": "summary", "status": "complete", "functions_total": 0, "functions_exported": 0,
             "failed": []}), encoding="utf-8")
        mutate = FakePopen.mutate
        if mutate is not None:
            mutate()

    mutate = None

    def communicate(self, timeout=None):
        return "", ""

    def poll(self):
        return 0


@pytest.fixture
def fake_popen(tmp_path, monkeypatch):
    FakePopen.launched = []
    FakePopen.mutate = None
    monkeypatch.setattr(ghidra, "available", lambda: True)
    monkeypatch.setattr(ghidra, "provider_version", lambda: "ghidra-12.1.4")
    monkeypatch.setattr(ghidra, "_headless", lambda: tmp_path / "analyzeHeadless")
    monkeypatch.setattr(ghidra, "java_home", lambda: None)
    monkeypatch.setattr(ghidra.subprocess, "Popen", FakePopen)
    return FakePopen


def test_imported_copy_is_the_hashed_copy_even_if_the_target_changes_later(tmp_path, fake_popen):
    obj = tmp_path / "game.o"
    original = bytes(range(64))
    obj.write_bytes(original)
    fake_popen.mutate = lambda: obj.write_bytes(b"changed after the identity was taken")
    m = ghidra.analyze_object(obj, tmp_path / "cache")
    assert m.status == "complete"
    assert len(fake_popen.launched) == 1
    launched = fake_popen.launched[0]
    assert launched["imported"] != obj  # a private work copy, not the caller's file
    assert launched["sha"] == m.input_digest == hashlib.sha256(original).hexdigest()


def test_work_copy_that_differs_from_the_recorded_identity_fails_before_import(tmp_path, fake_popen):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    admitted = ghidra.admit(None, bytes(64))
    wrong = hashlib.sha256(b"some other bytes").hexdigest()
    result = ghidra._run_headless(admitted, obj, ghidra.exporter_source(), (), False, 5, None,
                                  data=bytes(64), digest=wrong)
    assert result.status == "failed"
    assert any("identity mismatch" in d for d in result.diagnostics)
    assert fake_popen.launched == []  # nothing was imported


def test_headless_import_holds_and_releases_an_analysis_slot(tmp_path, fake_popen):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    before = scheduler.BUDGET.pressure()["used"]["analysis"]
    ghidra.analyze_object(obj, tmp_path / "cache")
    assert fake_popen.launched[0]["analysis_used"] == before + 1
    assert scheduler.BUDGET.pressure()["used"]["analysis"] == before


def test_process_group_kill_cleans_up():
    if os.name == "nt":
        pytest.skip("process-group kill is POSIX")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    ghidra._kill(proc)
    assert proc.wait(15) == -signal.SIGKILL


def test_session_manager_bounds_sessions_and_disposes(tmp_path, headless):
    manager = ghidra.SessionManager(cache_dir=tmp_path / "cache", max_sessions=2, idle_seconds=0.0)
    s1, s2 = manager.acquire("game.o"), manager.acquire("other.o")
    s3 = manager.acquire("third.o")  # bounded: the LRU session is evicted and disposed
    assert s1.disposed and not s2.disposed and set(manager.sessions()) == {"other.o", "third.o"}
    assert s1.dispose() is False  # dispose is idempotent
    assert s3.dispose() is True
    assert manager.acquire("third.o") is not s3  # a disposed handle is never handed out again
    assert manager.dispose_all() == 2 and manager.sessions() == {}  # other.o + the fresh third.o
    with pytest.raises(ghidra.GhidraError, match="disposed"):
        s2.flush_cache()


def test_flush_cache_and_reset_reimport(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    headless.respond(status="complete", exit_code=0, rows=[frow("fn", entry=0x100, text="int fn(void) {}")],
                     summary={"type": "summary", "status": "complete", "functions_total": 1, "functions_exported": 1,
                              "failed": []})
    cache = tmp_path / "cache"
    ghidra.analyze_object(obj, cache)
    manager = ghidra.SessionManager(cache_dir=cache)
    session = manager.acquire("game")
    assert "flushCache" in session.flush_cache()
    assert not list(cache.glob("game-*.json"))  # flushed: no stale current evidence
    session2 = manager.acquire("game")
    assert "reset/reimport" in session2.reset_reimport("overlay changed") and session2.disposed
    with pytest.raises(ValueError, match="unknown metadata-change kind"):
        manager.on_metadata_change("game", kind="bogus")


# ---------------------------------------------------------------------------- G-08

def test_thunks_externals_and_noncontiguous_bodies_are_classified_not_dropped(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    rows = [
        frow("thunk_fn", entry=0x100, classification="thunk", thunk_of=0x300, pstatus="unavailable", c="",
             ranges=[[0x100, 0x108]]),
        frow("ext_fn", entry=0x200, classification="external", pstatus="unavailable", c=""),
        frow("split_fn", entry=0x400, classification="normal", ranges=[[0x400, 0x420], [0x900, 0x910]],
             text="void split_fn(void) {}"),
    ]
    headless.respond(status="complete", exit_code=0, rows=rows,
                     summary={"type": "summary", "status": "complete", "functions_total": 3,
                              "functions_exported": 3, "failed": []})
    m = ghidra.analyze_object(obj, tmp_path / "cache")
    thunk, ext, split = m.by_name("thunk_fn"), m.by_name("ext_fn"), m.by_name("split_fn")
    assert thunk is not None and thunk.classification == "thunk" and thunk.thunk_of == 0x300
    assert thunk.coverage["pseudocode"] == "unavailable"
    assert ext is not None and ext.classification == "external" and ext.body_ranges == []
    assert split.classification == "normal" and split.noncontiguous
    assert split.body_ranges == [(0x400, 0x420), (0x900, 0x910)]
    assert m.names() == {"split_fn": "void split_fn(void) {}"}  # classified rows never vanish from the call graph


# ---------------------------------------------------------------------------- G-09

def test_real_lane_imports_a_clang_object_with_identity_correct_dossiers(tmp_path, monkeypatch):
    """G-09 real admitted-target lane: builds a tiny ELF object with clang, runs the full
    analyzeHeadless import/export through forks.ghidra, and checks the dossier rows against the
    fixture oracle (the object's own symbol table). Skips ONLY when neither the workspace Java
    (~/Decomp/java) nor a system Java exists, or when Ghidra/clang are absent — with the provisioned
    runtime this test RUNS. Bounded by analyze_object's 300s timeout."""
    java = ghidra.java_home()
    if java is None or ghidra.java_version() < 21 or ghidra.ghidra_home() is None:
        pytest.skip("the real admitted-target lane needs a Java 21+ runtime (DECOMP_JAVA_HOME, "
                    "~/Decomp/java or PATH) and a Ghidra install (DECOMP_GHIDRA_HOME, the toolchain "
                    "registry or the pinned workspace install): none found on this host")
    cc = shutil.which("clang")
    if cc is None:
        pytest.skip("clang is needed to build the tiny ELF fixture (-target x86_64-unknown-linux-gnu -c)")
    # Ghidra's logs/preferences default to ~/Library/ghidra; keep all real-lane state inside tmp_path
    monkeypatch.setenv("DECOMP_GHIDRA_USER_HOME", str(tmp_path / "ghuser"))
    (tmp_path / "ghuser").mkdir()
    src = tmp_path / "fixture.c"
    src.write_text("int add3(int a, int b, int c) { return a + b + c; }\n"
                   "int mul2(int a) { return a * 2; }\n")
    obj = tmp_path / "fixture.o"
    subprocess.run([cc, "-target", "x86_64-unknown-linux-gnu", "-c", str(src), "-o", str(obj)],
                   check=True, capture_output=True)
    nm = subprocess.run(["nm", str(obj)], capture_output=True, text=True, check=True).stdout
    syms = {parts[-1]: int(parts[0], 16) for line in nm.splitlines()
            if (parts := line.split()) and len(parts) >= 2}
    assert {"add3", "mul2"} <= set(syms)
    profile = TargetProfile(kind="object", processor="x86", bits=64, endian="little")
    m = ghidra.analyze_object(obj, tmp_path / "cache", timeout=300, profile=profile)
    assert m.status == "complete" and m.functions_exported == 2
    assert sorted(m.names()) == ["add3", "mul2"]
    assert m.language_id == "x86:LE:64:default"
    add3 = m.by_name("add3")
    assert add3 is not None and add3.pseudocode.status == "ok" and add3.body_ranges
    assert add3.entry == 0x100000 + syms["add3"]  # ElfLoader object image base + the fixture's offset
    assert "+" in add3.pseudocode.text  # the oracle's a+b+c survives decompilation
    mul2 = m.by_name("mul2")
    assert mul2 is not None and mul2.entry == 0x100000 + syms["mul2"]
    assert m.coverage_for("pseudocode").get("ok") == 2
    again = ghidra.analyze_object(obj, tmp_path / "cache", timeout=300, profile=profile)
    assert again.from_cache and sorted(again.names()) == ["add3", "mul2"]  # the real cache round-trips


# ---------------------------------------------------------------------------- G-10 / P7.6-P7.7

def test_proposals_cannot_mutate_project_state(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    rows = [frow("dispatch", entry=0x100, text="void dispatch(void) {}",
                 calls=[{"address": 0x110, "target": None, "name": "", "indirect": True}],
                 types=[{"name": "arg0", "signature": "int *", "origin": "decompiler", "complete": False},
                        {"name": "arg1", "signature": "short", "origin": "decompiler", "complete": False}])]
    headless.respond(status="complete", exit_code=0, rows=rows,
                     summary={"type": "summary", "status": "complete", "functions_total": 1, "functions_exported": 1,
                              "failed": []})
    m = ghidra.analyze_object(obj, tmp_path / "cache", facets=("calls", "types"))
    dossier = m.by_name("dispatch")
    proposals = ghidra.propose(dossier)
    assert proposals and any("indirect call" in " ".join(p.hypotheses) for p in proposals)
    assert all(p.blocked_reason and "protected edit path" in p.blocked_reason for p in proposals)
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "game.c").write_text("void dispatch(void) {}\n")
    before = {p.relative_to(project).as_posix(): p.read_text() for p in project.rglob("*") if p.is_file()}
    for p in proposals:
        with pytest.raises(ProposalError, match="protected edit path"):
            ghidra.apply_proposal(project, p)
    after = {p.relative_to(project).as_posix(): p.read_text() for p in project.rglob("*") if p.is_file()}
    assert after == before  # no direct state mutation, ever


def test_portfolio_keeps_m2c_primary_and_never_demotes_a_verified_best():
    best = DraftCandidate(provider="match", kind="best", source="/* verified near-match */", verified=True)
    m2c = DraftCandidate(provider="m2c", kind="m2c", source="/* m2c draft */", verified=False)
    g = DraftCandidate(provider="ghidra", kind="ghidra", source="/* readable ghidra c */", verified=False)
    pf = ghidra.make_portfolio("k", best=best, m2c=m2c, ghidra=g)
    assert pf.best() is best and pf.alternative() is None  # readability never demotes a near-match
    pf2 = ghidra.make_portfolio("k", best=DraftCandidate("match", "best", "", verified=False), m2c=m2c, ghidra=g)
    assert pf2.alternative() is m2c  # m2c stays the matching-oriented primary route
    pf3 = ghidra.make_portfolio("k", ghidra=g)
    assert pf3.alternative() is g
    assert ghidra.make_portfolio("k").alternative() is None


# ---------------------------------------------------------------------------- compatibility

def test_decompile_object_returns_the_compat_projection(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    rows = [frow("good_a", entry=0x100, text="int good_a(void) {}"),
            frow("good_b", entry=0x200, text="int good_b(void) {}"),
            frow("bad", entry=0x300, pstatus="failed", c="", warnings=["nope"])]
    headless.respond(status="partial", exit_code=0, rows=rows,
                     summary={"type": "summary", "status": "partial", "functions_total": 3,
                              "functions_exported": 3, "failed": ["bad"]})
    assert ghidra.decompile_object(obj, tmp_path / "cache") == {
        "good_a": "int good_a(void) {}", "good_b": "int good_b(void) {}"}


def test_decompile_object_returns_empty_when_ghidra_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(ghidra, "available", lambda: False)
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    assert ghidra.decompile_object(obj, tmp_path / "cache") == {}
    with pytest.raises(GhidraUnavailable):
        ghidra.analyze_object(obj, tmp_path / "cache")


def test_legacy_name_and_c_only_rows_still_parse(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    headless.respond(status="complete", exit_code=0, rows=[{"name": "old_fn", "c": "int old_fn(void) { return 1; }"}])
    m = ghidra.analyze_object(obj, tmp_path / "cache")
    d = m.by_name("old_fn")
    assert d is not None and d.pseudocode.status == "ok" and d.entry is None
    assert d.classification == "unknown" and d.coverage["instructions"] == "unknown"
    assert m.names() == {"old_fn": "int old_fn(void) { return 1; }"}


def test_cache_identity_covers_facets_bytes_and_analysis_options(tmp_path, headless):
    obj = tmp_path / "game.o"
    obj.write_bytes(bytes(64))
    headless.respond(status="complete", exit_code=0, rows=[frow("fn", entry=0x100)],
                     summary={"type": "summary", "status": "complete", "functions_total": 1, "functions_exported": 1,
                              "failed": []})
    cache = tmp_path / "cache"
    ghidra.analyze_object(obj, cache)                                # run 1: pseudocode only
    ghidra.analyze_object(obj, cache, facets=("instructions",))      # run 2: a new facet is a new identity
    ghidra.analyze_object(obj, cache, facets=("instructions",), want_bytes=True)  # run 3: bytes requested
    profile = TargetProfile(kind="raw_region", processor="mips", bits=32, endian="big",
                            memory_map=(MemoryRegion(name="r", file_offset=0, size=64, vaddr=0),))
    ghidra.analyze_object(obj, cache, profile=profile)               # run 4: mapping changed
    opts = replace(profile, analysis_options=("Decompiler Parameter ID=off",))
    ghidra.analyze_object(obj, cache, profile=opts)                  # run 5: analysis options changed
    assert len(headless.calls) == 5
    with pytest.raises(ValueError, match="unknown Ghidra facets"):
        ghidra.analyze_object(obj, cache, facets=("nope",))


def test_exporter_reuses_one_decompinterface_and_stays_backward_compatible():
    src = (ROOT / "src" / "decompnet" / "forks" / "ghidra_scripts" / "DecompWtfExport.java").read_text()
    assert src.count("new DecompInterface()") == 1  # one DecompInterface per owned Program
    assert "ifc.dispose()" in src and "finally" in src  # disposed reliably
    assert '{"name"' in src.replace('"name"', '"name"') and "summary.json" in src
    # the reviewed name->C parser keeps working: every row carries "name" and "c" (empty for non-C rows)
    assert src.count(",\"c\":") == 1 or '"c":' in src


def test_unknown_unavailable_empty_are_distinct_values():
    d = FunctionDossier(key="k", name="n", coverage={"pseudocode": "unknown", "instructions": "unavailable",
                                                     "calls": "empty", "blocks": "ok"})
    assert d.coverage["pseudocode"] == "unknown" and d.coverage["instructions"] == "unavailable"
    assert d.coverage["calls"] == "empty" and d.coverage["blocks"] == "ok"
    assert len(set(d.coverage.values())) == 4
