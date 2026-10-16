"""PR-15 (plan P4.2-P4.4): real m2c context preparation, content-addressed context/draft caches,
and visible fallback quality. Covers D-01..D-05 and D-08, plus a real-m2c smoke round-trip
(gated on the installed toolchain, skipped cleanly otherwise).

The unit tests replace the preprocessor and m2c subprocesses (seeds._run_cpp / seeds._run_m2c_process)
so they pass on any host; D-01/D-02 exercise the real host preprocessor and skip when none exists.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from decompnet import seeds
from decompnet.seeds import STATUS_COMPLETE, STATUS_DEGRADED, STATUS_OMITTED

needs_cpp = pytest.mark.skipif(shutil.which("cpp") is None and shutil.which("clang") is None
                               and shutil.which("gcc") is None, reason="needs a C preprocessor")
needs_m2c = pytest.mark.skipif(seeds.available() is False, reason="needs m2c")

ASM_RET3 = "glabel f\naddiu $v0, $zero, 3\njr $ra\nnop\n"


def _fake_preprocessor(monkeypatch, header: str) -> None:
    """A preprocessor that passes text through (no host cpp dependency), and records nothing."""
    def fake_cpp(argv, timeout=30):
        return subprocess.CompletedProcess(argv, 0, header, "")

    monkeypatch.setattr(seeds, "_run_cpp", fake_cpp)
    monkeypatch.setattr(seeds, "_host_preprocessor", lambda dialect: (Path("fake-cpp"), []))


def _fake_m2c(monkeypatch, text: str, reject_context: bool = False, calls: list | None = None):
    """An m2c stand-in: canned output, optionally rejecting any context run like a parse failure."""
    calls = calls if calls is not None else []

    def fake(argv, timeout):
        calls.append(list(argv))
        if reject_context and "--context" in argv:
            return 1, ("/*\nDecompilation failure:\n\nSyntax error when parsing C context.\n"
                       "before: ; at line 3, column 14\n\nint broken = ;\n*/"), ""
        return 0, text, ""

    monkeypatch.setattr(seeds, "_run_m2c_process", fake)
    return calls


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------------- D-01

@needs_cpp
def test_D01_conditionals_and_includes_produce_active_definitions(tmp_path):
    (tmp_path / "a.h").write_text("int from_a;\n", encoding="utf-8")
    header = ('#include "a.h"\n'
              "#if FOO == 2\n"
              "int active_x2;\n"
              "#else\n"
              "int active_x1;\n"
              "#endif\n")
    prep = seeds.prepare_context(header, defines={"FOO": "2"}, include_dirs=[tmp_path],
                                 target="mips-gcc-c")
    assert prep.text and "int active_x2;" in prep.text
    assert "int active_x1;" not in prep.text          # the inactive branch is gone
    assert "int from_a;" in prep.text                 # the include really expanded
    assert "#include" not in prep.text and "#if FOO" not in prep.text
    manifest = prep.manifest
    inputs = manifest["inputs"]
    assert inputs[0] == {"role": "header", "path": None, "sha256": _sha(header)}
    includes = [i for i in inputs if i["role"] == "include"]
    assert any(i["path"].endswith("a.h") and i["sha256"] == _sha("int from_a;\n") for i in includes)
    assert "-DFOO=2" in manifest["command"]           # the preprocessor command is retained
    assert manifest["defines"] == {"FOO": "2"}
    assert manifest["adapter"]["kind"] == "host"      # a substitute adapter is honestly labelled...
    assert prep.status == STATUS_DEGRADED             # ...as degraded, never as complete


# ----------------------------------------------------------------------------------- D-02

@needs_cpp
def test_D02_multiline_macros_and_legacy_extensions_preserved_or_declared(tmp_path):
    header = ("#define SUM(a, b) (a) + \\\n"
              "                   (b)\n"
              "typedef struct __attribute__((packed)) P { char c; int i; } P;\n"
              "int v = SUM(1, 2);\n"
              "typeof(v) w;\n")
    prep = seeds.prepare_context(header, target="mips-gcc-c")
    flat = "".join(prep.text.split())
    assert "(1)+(2)" in flat                          # the multiline macro really expanded
    assert "#define" not in prep.text
    assert "__attribute__((packed))" in prep.text.replace(" ", "") or "packed" in prep.text
    # typeof is converted by a narrow tested rule (__typeof__ keeps width/signedness exactly)
    import re
    assert re.search(r"\btypeof\s*\(", prep.text) is None
    assert "__typeof__(v)" in flat
    assert prep.manifest["conversions"] == [
        {"rule": "normalize-gnu-typeof", "count": 1,
         "note": "bare `typeof` rewritten to `__typeof__`, which m2c's parser accepts "
                 "(type width/signedness unchanged)"}]
    assert prep.status == STATUS_DEGRADED             # a conversion was applied: degraded, visible


@needs_cpp
def test_D02_cpp_constructs_are_declared_degraded_never_silently_wrong(tmp_path):
    header = "template <typename T> struct S { T x; };\nnamespace ns { int y; }\n"
    prep = seeds.prepare_context(header, dialect="c++", target="mips-gcc-c++")
    assert prep.status == STATUS_DEGRADED
    assert "template" in prep.text and "namespace" in prep.text  # left as-is, not guessed at
    warns = {w["construct"]: w for w in prep.manifest["warnings"]}
    assert "template" in warns and "namespace" in warns


def test_D02_toolchain_cpp_failure_falls_back_to_a_labelled_substitute(tmp_path, monkeypatch):
    monkeypatch.setattr(seeds, "_toolchain_cpp", lambda tid: Path("/nonexistent/cpp"))
    monkeypatch.setattr(seeds, "_host_preprocessor", lambda dialect: (Path("host-cpp"), []))

    def fake_cpp(argv, timeout=30):
        if argv[0] == "/nonexistent/cpp":
            return subprocess.CompletedProcess(argv, 1, "", "ctx.c:3: bogus directive\n")
        return subprocess.CompletedProcess(argv, 0, "int ok;\n", "")

    monkeypatch.setattr(seeds, "_run_cpp", fake_cpp)
    prep = seeds.prepare_context("int ok;\n", target="mips-gcc-c")
    assert prep.status == STATUS_DEGRADED             # the fallback is labelled, never silent
    assert prep.manifest["adapter"]["kind"] == "host"
    failed = prep.manifest["preprocessor_attempts_failed"]
    assert failed and any(d.get("line") == 3 and d.get("file") == "ctx.c" for d in failed)
    assert "int ok;" in prep.text


# ----------------------------------------------------------------------------------- D-03

def test_D03_context_parse_failure_falls_back_labelled(tmp_path, monkeypatch):
    _fake_preprocessor(monkeypatch, "int broken = ;\n")
    _fake_m2c(monkeypatch, "int f(void) { return 1; }\n", reject_context=True)
    result = seeds.draft(ASM_RET3, "f", "gcc-2.7.2-kmc", context_header="int broken = ;\n",
                         cache_root=tmp_path / "cache")
    assert result.outcome == "ok"                     # the context-free retry still produced a draft
    assert result.text == "int f(void) { return 1; }"
    assert result.context_status == STATUS_OMITTED    # ...but it is labelled omitted
    assert result.fallback is True
    assert result.dependencies["context"]["digest"]   # the rejected context's identity is retained
    # the diagnostic is retained, with the source location m2c reported
    assert any(d.get("line") == 3 and "context" in d["message"] for d in result.diagnostics)
    annotated = result.annotated_text() or ""
    assert "fallback" in annotated and "omitted" in annotated


def test_D03_failed_preparation_is_also_labelled(tmp_path, monkeypatch):
    monkeypatch.setattr(seeds, "_toolchain_cpp", lambda tid: None)
    monkeypatch.setattr(seeds, "_host_preprocessor", lambda dialect: None)  # no preprocessor at all
    _fake_m2c(monkeypatch, "int f(void) { return 2; }\n")
    result = seeds.draft(ASM_RET3, "f", "gcc-2.7.2-kmc", context_header="typedef s32 A;\n",
                         cache_root=tmp_path / "cache")
    assert result.outcome == "ok" and result.text == "int f(void) { return 2; }"
    assert result.context_status == STATUS_OMITTED and result.fallback is True
    assert any("preprocessor" in d["message"] for d in result.diagnostics)


# ----------------------------------------------------------------------------------- D-04

def test_D04_repeated_prepared_contexts_reuse_the_cache(tmp_path, monkeypatch):
    header = "typedef struct { s32 x; } S;\n"
    calls = []
    monkeypatch.setattr(seeds, "_host_preprocessor", lambda dialect: (Path("fake-cpp"), []))

    def counting_cpp(argv, timeout=30):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, header, "")

    monkeypatch.setattr(seeds, "_run_cpp", counting_cpp)
    cache_root = tmp_path / "cache"
    prep1 = seeds.prepare_context(header, target="mips-gcc-c", cache_root=cache_root)
    seeds._PREPARED_MEMO.clear()                      # force the filesystem path again
    prep2 = seeds.prepare_context(header, target="mips-gcc-c", cache_root=cache_root)
    assert prep1.digest == prep2.digest               # content identity, not a fresh temp dir
    assert prep1.directory == prep2.directory
    assert len(calls) == 2                            # cpp ran again (memo cleared)...
    done = json.loads((prep2.directory / "DONE").read_text(encoding="utf-8"))
    assert done["digest"] == prep2.digest
    assert done["text_sha256"] == _sha(header)        # ...but the published dir was reused intact
    assert json.loads((prep2.directory / "manifest.json").read_text(encoding="utf-8"))["command"]
    # identical content published concurrently still lands in one identical dir
    preps = []
    def worker():
        preps.append(seeds.prepare_context(header, target="mips-gcc-c", cache_root=cache_root))
    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {p.digest for p in preps} == {prep1.digest}
    assert {p.directory for p in preps} == {prep1.directory}


def test_D04_identical_draft_requests_reuse_the_draft_cache_and_coalesce(tmp_path, monkeypatch):
    _fake_preprocessor(monkeypatch, "typedef s32 A;\n")
    calls = []
    _fake_m2c(monkeypatch, "s32 f(void) { return 7; }\n", calls=calls)
    args = dict(asm_text=ASM_RET3, name="f", toolchain_id="gcc-2.7.2-kmc",
                context_header="typedef s32 A;\n", cache_root=tmp_path / "cache")
    r1 = seeds.draft(**args)
    r2 = seeds.draft(**args)
    assert r1.text == r2.text == "s32 f(void) { return 7; }"
    assert r1.cached is False and r2.cached is True     # the second came from the cache
    assert len(calls) == 1                              # m2c ran exactly once

    # concurrent identical requests on a cold key: one producer, every waiter served
    release = threading.Event()
    started = threading.Event()
    lock = threading.Lock()

    def slow_fake(argv, timeout):
        with lock:
            calls.append(1)
        started.set()
        release.wait(5.0)
        return 0, "s32 g(void) { return 9; }\n", ""

    monkeypatch.setattr(seeds, "_run_m2c_process", slow_fake)
    results = []
    def worker():
        results.append(seeds.draft(**{**args, "name": "g"}))
    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    assert started.wait(5.0)
    release.set()
    for t in threads:
        t.join()
    assert all(r.text == "s32 g(void) { return 9; }" for r in results)
    assert len(calls) == 2                              # the f draft plus one coalesced g draft


# ----------------------------------------------------------------------------------- D-05

def test_D05_context_target_data_and_compiler_changes_invalidate_drafts(tmp_path, monkeypatch):
    _fake_preprocessor(monkeypatch, "typedef s32 A;\n")
    _fake_m2c(monkeypatch, "s32 f(void) { return 5; }\n")
    base = dict(asm_text=ASM_RET3, name="f", toolchain_id="gcc-2.7.2-kmc",
                context_header="typedef s32 A;\n", cache_root=tmp_path / "cache")
    r1 = seeds.draft(**base)
    key1, ctx1 = r1.dependencies["draft_key"], r1.dependencies["context"]["digest"]
    assert key1 and ctx1

    r2 = seeds.draft(**{**base, "context_header": "typedef s32 B;\n"})  # header changed
    assert r2.dependencies["draft_key"] != key1
    assert r2.dependencies["context"]["digest"] != ctx1

    r3 = seeds.draft(**{**base, "asm_text": ASM_RET3 + "nop\n"})        # target bytes changed
    assert r3.dependencies["draft_key"] != key1

    r4 = seeds.draft(**base, data_asm=".word 0\n")                      # target data changed
    assert r4.dependencies["draft_key"] != key1

    r5 = seeds.draft(**{**base, "toolchain_id": "gcc-2.7.2-psx"})       # compiler profile changed
    assert r5.dependencies["draft_key"] != key1

    r6 = seeds.draft(**base, valid=True)                                # draft options changed
    assert r6.dependencies["draft_key"] != key1

    # old drafts stay identifiable and explicitly historical: the original identity still resolves
    r7 = seeds.draft(**base)
    assert r7.dependencies["draft_key"] == key1 and r7.cached is True
    assert r7.dependencies["context"]["digest"] == ctx1
    assert r7.dependencies["m2c"]["version"]                            # exact dependency identities


# ----------------------------------------------------------------------------------- D-08

def test_D08_unresolved_placeholders_remain_explicit(tmp_path, monkeypatch):
    _fake_preprocessor(monkeypatch, "typedef s32 X;\n")
    draft_text = ("s32 f(void) {\n"
                  "    M2C_UNK32 tmp;\n"
                  "    tmp = M2C_BITWISE(u32, M2C_CARRY + x);\n"
                  "    M2C_ERROR(\"cannot convert\");\n"
                  "    return M2C_OVERFLOW(tmp) + tmp;\n"
                  "}\n")
    _fake_m2c(monkeypatch, draft_text)
    result = seeds.draft(ASM_RET3, "f", "gcc-2.7.2-kmc", context_header="typedef s32 X;\n",
                         valid=True, cache_root=tmp_path / "cache")
    assert result.outcome == "ok"                     # the syntax compiles...
    kinds = {u["kind"]: u for u in result.unresolved}
    assert {"unknown-type", "unresolved-bitwise", "unresolved-carry", "unresolved-overflow",
            "unresolved-error"} <= set(kinds)
    assert kinds["unknown-type"]["total"] == 1 and kinds["unknown-type"]["line"] == 2
    # ...but compilability is not acceptance: readiness stays unknown, placeholders stay explicit
    assert result.compilation_ready == seeds.READINESS_UNKNOWN
    annotated = result.annotated_text() or ""
    assert "unresolved placeholders" in annotated and "unknown-type" in annotated

    # a non-valid draft audits nothing but also never claims readiness
    result2 = seeds.draft(ASM_RET3, "f", "gcc-2.7.2-kmc", context_header="typedef s32 X;\n",
                          valid=False, cache_root=tmp_path / "cache")
    assert result2.unresolved == []
    assert result2.compilation_ready == seeds.READINESS_UNKNOWN


# ----------------------------------------------------------------------------------- real lane

@needs_m2c
def test_real_m2c_draft_roundtrips_through_the_context_cache(tmp_path):
    from decompnet import toolchains
    if toolchains.installed("gcc-2.7.2-kmc") is None and shutil.which("cpp") is None:
        pytest.skip("needs a preprocessor (the toolchain's own cpp or a host cpp)")
    header = "typedef struct P { s32 x; s32 y; } P;\nextern P g_p;\n"
    cache_root = tmp_path / "cache"
    args = dict(asm_text=ASM_RET3, name="f", toolchain_id="gcc-2.7.2-kmc",
                context_header=header, cache_root=cache_root)
    r1 = seeds.draft(**args)
    assert r1.outcome == "ok" and r1.text and "return 3" in r1.text
    assert r1.context_status in (STATUS_COMPLETE, STATUS_DEGRADED)
    assert r1.dependencies["context"]["digest"]
    # the prepared context lives in its content-addressed dir, and m2c's own typemap cache sits beside it
    ctx_dir = cache_root / "contexts" / r1.dependencies["context"]["digest"]
    assert (ctx_dir / "DONE").is_file() and (ctx_dir / "manifest.json").is_file()
    assert json.loads((ctx_dir / "manifest.json").read_text(encoding="utf-8"))["command"]
    assert (ctx_dir / "context.c.m2c").is_file()   # m2c's own typemap cache is reused (P4.3)
    r2 = seeds.draft(**args)
    assert r2.cached is True and r2.text == r1.text     # the whole draft round-tripped through the cache
    # a context m2c cannot parse degrades to the labelled context-free retry, diagnostic retained
    r3 = seeds.draft(**{**args, "context_header": "int broken = ;\n"})
    assert r3.outcome == "ok" and "return 3" in r3.text
    assert r3.context_status == STATUS_OMITTED and r3.fallback is True
    assert r3.dependencies["context"]["digest"]     # the rejected context's identity is retained
    assert any("context" in d["message"] and d.get("line") is not None and d.get("column") == 14
               for d in r3.diagnostics)
