"""Machine-generated starting points: run m2c on a function's target code to give the first agent
a C draft instead of bare assembly.

m2c (GPL-3.0) is installed as a separate pinned tool (`decompnet toolchain install m2c`) and run as a
program, never imported. It reads GNU-as syntax, which we rebuild from the target object: capstone
disassembly with relocations shown as %hi/%lo/symbols and branch targets turned into labels.

m2c's `--context` expects a file *already processed by the C preprocessor* (it strips the remaining
`#define`s itself and never resolves `#include`s). The legacy behaviour here was to drop preprocessor
directive lines, which silently broke conditional headers, multiline macros and includes (plan D06).
Since PR-15 the context is prepared properly:

* prepare_context() gathers the unit's header, include directories and defines and preprocesses them
  in a controlled environment: the selected legacy toolchain's own `cpp` when it is installed, else a
  host `cpp` with `-undef -nostdinc -traditional-cpp` — a substitute adapter, and therefore a
  *degraded* preparation, never a silent one. Only unsupported parser constructs are converted, via
  narrow tested rules (plan P4.2), and a manifest of every input read plus the exact preprocessor
  command is retained.
* The prepared context is published into a content-addressed directory (prepared text + manifest +
  m2c version/target/dialect) with locking and atomic publication, so one invocation never overwrites
  another revision's files; m2c's own typemap cache (`context.c.m2c`) is then stable and reusable
  (plan P4.3).
* Drafts are cached content-addressed by target asm+data, context identity, m2c version, target mode
  and draft options. A seed created under an old header is not current merely because the target
  bytes did not change.
* draft() returns a DraftResult that makes fallback quality visible: provider identity, context
  status (complete / degraded / omitted), compilation readiness, unresolved placeholder constructs,
  warnings and exact dependency identities (plan P4.4). The context-free retry stays — an unparsable
  context must not cost us the seed — but it is now labelled instead of silently erased.

generate() / generate_from_asm() / run_m2c() keep their text-only contracts for existing callers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import cache, scoring, toolchains
from .elfobj import FunctionObj

# m2c target triple per toolchain (platform-compiler-language).
M2C_TARGETS = {
    "gcc-2.7.2-kmc": "mips-gcc-c",
    "ee-gcc-2.96": "mipsee-gcc-c",
    "ido-7.1": "mips-ido-c",
    "ido-5.3": "mips-ido-c",
    "gcc-2.7.2-psx": "mipsel-gcc-c",
    "gcc-2.8.1-psx": "mipsel-gcc-c",
    "agbcc": "gba-gcc-c",
}
# What m2c's --valid-syntax output needs to compile: its unknown types and macros, defined the simplest way
# that keeps the code's meaning (written for decomp.wtf; the names are m2c's interface). These are
# *placeholders for unresolved semantics*, not established behaviour: M2C_UNK* marks an unknown type,
# M2C_FIELD a guessed field offset, M2C_BITWISE/M2C_CARRY/M2C_OVERFLOW guessed bit/carry/overflow
# semantics, and M2C_ERROR something m2c could not express at all. DraftResult.unresolved reports every
# use of them; compilable draft syntax is not proof of correct behaviour (the verifier still decides).
M2C_MACROS = """\
#ifndef M2C_DEFINES
#define M2C_DEFINES
typedef s32 M2C_UNK;
typedef s8 M2C_UNK8;
typedef s16 M2C_UNK16;
typedef s32 M2C_UNK32;
#define M2C_FIELD(expr, type_ptr, offset) (*(type_ptr)((s8 *)(expr) + (offset)))
#define M2C_BITWISE(type, expr) ((type)(expr))
#define M2C_ERROR(desc) (0)
#define M2C_CARRY 0
#define M2C_OVERFLOW(a) (0)
#endif
"""
_BRANCH = re.compile(r"^(b|beq|bne|beqz|bnez|bgtz|blez|bltz|bgez|bltzal|bgezal|beql|bnel|beqzl|bnezl|bgtzl|blezl|"
                     r"bltzl|bgezl|bc1t|bc1f|bc1tl|bc1fl|j)$")

CONTEXT_SCHEMA = 1     # bump when the prepared-context identity changes meaning
DRAFT_KEY_SCHEMA = 1   # bump when draft cache keys change meaning
STATUS_COMPLETE = "complete"
STATUS_DEGRADED = "degraded"
STATUS_OMITTED = "omitted"
READINESS_KNOWN = "known"
READINESS_UNKNOWN = "unknown"
_MAX_DIAGNOSTICS = 64
_MAX_WARNINGS = 32
_MAX_MANIFEST_INPUTS = 256
_MAX_LOG_LINE = 500
_CPP_TIMEOUT = 30.0
_CACHE_ENV = "DECOMPNET_M2C_CACHE"


def available() -> bool:
    return toolchains.installed("m2c") is not None


def gnu_asm(fn: FunctionObj) -> str:
    """The function as a GNU as source file m2c understands."""
    insns = scoring.disassemble(fn)
    targets: set[int] = set()
    body = []
    for ins in insns:
        ops = ins.operands
        parts = [p.strip() for p in ops.split(",")] if ops else []
        if _BRANCH.match(ins.mnemonic) and parts and re.fullmatch(r"0x[0-9a-f]+|\d+", parts[-1]):
            target = int(parts[-1], 0)
            targets.add(target)
            parts[-1] = f".L{fn.name}_{target:X}"
            ops = ", ".join(parts)
        body.append((ins.offset, f"{ins.mnemonic} {ops}".rstrip()))
    lines = [".set noat", ".set noreorder", ".set gp=64", "", f"glabel {fn.name}"]
    for off, text in body:
        if off in targets:
            lines.append(f".L{fn.name}_{off:X}:")
        lines.append(f"/* {off:06X} */ {text}")
    return "\n".join(lines) + "\n"


# -----------------------------------------------------------------------------------
# Structured diagnostics (bounded logs with source locations, plan P4.2 item 7)
# -----------------------------------------------------------------------------------

def _diag(level: str, message: str, file: str | None = None, line: int | None = None,
          column: int | None = None) -> dict:
    return {"level": level, "message": str(message)[:_MAX_LOG_LINE], "file": file,
            "line": line, "column": column}


class ContextPreparationError(Exception):
    """No usable prepared context could be produced. `.diagnostics` carries the bounded,
    source-located reasons; callers label the resulting draft `omitted` and keep the details."""

    def __init__(self, reason: str, diagnostics: list[dict] | None = None):
        super().__init__(reason)
        self.diagnostics = diagnostics or [_diag("error", reason)]


@dataclass(frozen=True)
class PreparedContext:
    """A preprocessed, content-addressed m2c context (plan P4.2/P4.3)."""
    text: str          # preprocessed C (still holds #line markers and #pragmas)
    status: str        # STATUS_COMPLETE | STATUS_DEGRADED
    digest: str        # identity: text + manifest + m2c version/target/dialect
    directory: Path    # published content-addressed dir (context.c, manifest.json, DONE)
    manifest: dict     # every input read + the exact preprocessor command + conversions/warnings


# -----------------------------------------------------------------------------------
# P4.2 — context preparation
# -----------------------------------------------------------------------------------

def _m2c_version() -> str:
    m = toolchains.installed("m2c")
    if not m:
        return "uninstalled"
    return m.get("commit") or f"installed-at-{m.get('installed_at', 0)}"


def _toolchain_cpp(toolchain_id: str | None) -> Path | None:
    """The legacy toolchain's own preprocessor, when that toolchain is installed."""
    if not toolchain_id:
        return None
    for rel in ("cpp", "bin/cpp"):
        p = toolchains.path(toolchain_id) / rel
        if p.is_file():
            return p
    return None


def _host_preprocessor(dialect: str) -> tuple[Path, list[str]] | None:
    """A substitute host preprocessor in a controlled mode: no builtin defines, no system includes.
    For C++ the macOS `cpp` shim cannot take `-x c++`; use the compiler driver directly."""
    if dialect == "c":
        cpp = shutil.which("cpp")
        return (Path(cpp), ["-traditional-cpp"]) if cpp else None
    for name in ("clang", "gcc", "cc"):
        p = shutil.which(name)
        if p:
            return Path(p), ["-x", "c++"]
    return None


def _run_cpp(argv: list[str], timeout: float = _CPP_TIMEOUT) -> subprocess.CompletedProcess:
    """One preprocessor invocation (split out so tests can observe/replace it)."""
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


_CPP_LOCATION = re.compile(r"^([^:\s][^:]*):(\d+)(?::(\d+))?:")


def _cpp_failure_diags(cpp: Path, stderr: str) -> list[dict]:
    diags = []
    for line in (stderr or "").strip().splitlines()[-8:]:
        m = _CPP_LOCATION.match(line.strip())
        if m:
            diags.append(_diag("error", line.strip()[m.end():].strip() or line.strip(), m.group(1),
                               int(m.group(2)), int(m.group(3)) if m.group(3) else None))
        elif line.strip():
            diags.append(_diag("error", f"{cpp}: {line.strip()}"))
    return diags or [_diag("error", f"{cpp}: preprocessing failed")]


def _include_trace(stderr: str) -> list[str]:
    """Files the preprocessor reports reading (its -H trace), newest-to-oldest, deduplicated."""
    out: list[str] = []
    for line in (stderr or "").splitlines():
        tok = line.strip()
        while tok.startswith("."):
            tok = tok[1:].strip()
        if not tok or not tok.startswith(("/", "./", "../")):
            continue
        p = Path(tok)
        if p.is_file():
            out.append(str(p.resolve()))
    return list(dict.fromkeys(out))[:_MAX_MANIFEST_INPUTS]


# Narrow, tested compatibility rules for constructs m2c's parser cannot read. Everything else —
# __attribute__ (packed/aligned/...), #pragma pack, __signed__, __inline__, __extension__, asm labels —
# is understood by m2c's parser and is preserved as-is, so type widths, signedness, packing and enum
# values survive preparation.
_TYPEOF = re.compile(r"\btypeof\s*\(")
_CPP_CONSTRUCTS = {
    "template": re.compile(r"\btemplate\s*<"),
    "namespace": re.compile(r"\bnamespace\b"),
    "class-definition": re.compile(r"\bclass\s+[A-Za-z_]\w*\b"),
    "access-specifier": re.compile(r"\b(public|private|protected)\s*:"),
    "cxx-cast": re.compile(r"\b(?:static|dynamic|const|reinterpret)_cast\s*<"),
}


def _convert_for_parser(text: str, dialect: str) -> tuple[str, list[dict], list[dict]]:
    """Apply narrow, tested parser-compatibility conversions and list what could not be converted
    safely (never silently guessed: those keep the construct and are declared degraded)."""
    conversions: list[dict] = []
    warnings: list[dict] = []
    text, n = _TYPEOF.subn("__typeof__(", text)
    if n:
        conversions.append({"rule": "normalize-gnu-typeof", "count": n,
                            "note": "bare `typeof` rewritten to `__typeof__`, which m2c's parser accepts "
                                    "(type width/signedness unchanged)"})
    if dialect == "c++":
        for name, pat in _CPP_CONSTRUCTS.items():
            c = len(pat.findall(text))
            if c:
                warnings.append({"construct": name, "count": c,
                                 "note": "C++-only construct left as-is: no safe C equivalent; "
                                         "m2c's C parser may reject the context"})
    return text, conversions, warnings


def _context_digest(text: str, manifest: dict, m2c_version: str, target: str, dialect: str) -> str:
    return cache.sha(cache.canonical({
        "schema": CONTEXT_SCHEMA, "text": text, "manifest": manifest,
        "m2c_version": m2c_version, "target": target, "dialect": dialect,
    }))


def prepare_context(header: str, *, defines: dict[str, str | None] | None = None,
                    include_dirs: Sequence[str | Path] = (),
                    toolchain_id: str | None = None, dialect: str = "c",
                    target: str | None = None, m2c_version: str | None = None,
                    cache_root: Path | str | None = None) -> PreparedContext:
    """Preprocess the unit's header for m2c (plan P4.2): the legacy toolchain's own `cpp` when
    installed, else a controlled substitute adapter (labelled *degraded*). Returns the published
    content-addressed context; raises ContextPreparationError when nothing usable can be made.

    `defines` maps symbol -> value (None = defined with no value); `include_dirs` resolve `#include`s
    in order. The manifest records every input read plus the exact preprocessor command."""
    if not header.strip():
        raise ContextPreparationError("empty context header", [_diag("error", "empty context header")])
    if dialect not in ("c", "c++"):
        raise ContextPreparationError(f"unsupported dialect {dialect!r}",
                                      [_diag("error", f"unsupported dialect {dialect!r}")])
    cpp = _toolchain_cpp(toolchain_id)
    host = _host_preprocessor(dialect)
    if cpp is None and host is None:
        raise ContextPreparationError(
            "no C preprocessor available (neither the toolchain's own cpp nor a host cpp)",
            [_diag("error", "no C preprocessor available")])
    attempts: list[tuple[str, Path, list[str]]] = []
    if cpp is not None:
        attempts.append(("toolchain", cpp, []))
    if host is not None:
        attempts.append(("host", *host))
    m2c_version = m2c_version or _m2c_version()
    target = target or toolchain_id or "unknown"
    try:
        root = cache_root_for(cache_root)
    except OSError:
        root = None
    unit_name = "context.cpp" if dialect == "c++" else "context.c"

    adapter_kind = ""
    text: str | None = None
    trace_stderr = ""
    argv: list[str] = []
    attempt_errors: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="decompnet-cpp-") as td:
        src = Path(td) / unit_name
        src.write_text(header, encoding="utf-8")
        for kind, cpp_bin, extra in attempts:
            argv = [str(cpp_bin), "-E", "-undef", "-nostdinc", "-H", *extra]
            for k in sorted(defines or {}):
                argv.append(f"-D{k}={defines[k]}" if defines[k] is not None else f"-D{k}")
            for d in include_dirs:
                argv.append(f"-I{d}")  # attached form: the macOS cpp shim mishandles "-I dir"
            argv.append(str(src))
            try:
                res = _run_cpp(argv)
            except subprocess.TimeoutExpired:
                attempt_errors.append(_diag("error", f"preprocessor {cpp_bin} timed out", unit_name))
                continue
            if res.returncode != 0:
                attempt_errors.extend(_cpp_failure_diags(cpp_bin, res.stderr))
                continue
            adapter_kind, cpp = kind, cpp_bin
            trace_stderr = res.stderr
            # #line markers embed the per-call temp path; normalise to the logical unit name so the
            # prepared text (and therefore its content identity) is deterministic.
            text = res.stdout.replace(str(src), unit_name)
            break
        if text is None:
            raise ContextPreparationError(
                f"could not preprocess the context header (rc from every adapter)", attempt_errors)
        text, conversions, warnings = _convert_for_parser(text, dialect)

    degraded: list[str] = []
    if adapter_kind == "host":
        degraded.append(f"substitute preprocessor {cpp} used instead of "
                        f"{toolchain_id or 'the legacy compiler'}'s own")
    if attempt_errors:
        degraded.append("a preferred preprocessor failed; a substitute adapter succeeded")
    if conversions:
        degraded.append("parser-compatibility conversions applied")
    if warnings:
        degraded.append("constructs m2c's parser may not support remain")
    status = STATUS_COMPLETE if not degraded else STATUS_DEGRADED
    # The manifest is content identity as well as an audit record: the unit source is a per-call
    # temp file, so it is recorded under its logical name — the header entry carries the real sha.
    manifest = {
        "schema": CONTEXT_SCHEMA,
        "status": status,
        "degraded_reasons": degraded,
        "adapter": {"kind": adapter_kind, "cpp": str(cpp), "toolchain": toolchain_id},
        "command": argv[:-1] + [unit_name],
        "preprocessor_attempts_failed": attempt_errors[:_MAX_DIAGNOSTICS],
        "dialect": dialect,
        "defines": dict(sorted((defines or {}).items())),
        "include_dirs": [str(Path(d).resolve()) for d in include_dirs],
        "inputs": [{"role": "header", "path": None, "sha256": cache.sha(header.encode("utf-8"))},
                   {"role": "unit", "path": unit_name, "sha256": cache.sha(header.encode("utf-8"))}]
                  + [{"role": "include", "path": p, "sha256": cache.file_sha(Path(p))}
                     for p in _include_trace(trace_stderr)][:_MAX_MANIFEST_INPUTS],
        "conversions": conversions,
        "warnings": warnings[:_MAX_WARNINGS],
    }
    digest = _context_digest(text, manifest, m2c_version, target, dialect)

    prepared = _PREPARED_MEMO.get(digest)
    if prepared is not None:
        return prepared
    directory = None
    if root is not None:
        try:
            directory = _publish_context(root, digest, text, manifest)
        except OSError:
            directory = None  # the cache is an optimisation, never a requirement
    if directory is None:
        directory = _stage_context_temp(digest, text, manifest)
    prepared = PreparedContext(text, status, digest, directory, manifest)
    with _PREPARED_MEMO_LOCK:
        _PREPARED_MEMO[digest] = prepared
        while len(_PREPARED_MEMO) > _PREPARED_MEMO_MAX:
            _PREPARED_MEMO.pop(next(iter(_PREPARED_MEMO)))
    return prepared


# -----------------------------------------------------------------------------------
# P4.3 — content-addressed context directories and draft cache
# -----------------------------------------------------------------------------------

def cache_root_for(explicit: Path | str | None = None) -> Path | None:
    """The m2c cache root (contexts/ + drafts/). None disables both caches. `explicit` overrides the
    DECOMPNET_M2C_CACHE environment variable (`off`/`0` disables); the default is the per-user
    decompnet data dir."""
    value: str | Path | None = explicit
    if value is None:
        env = os.environ.get(_CACHE_ENV)
        value = env if env else None
    if value is None or str(value).strip().lower() in ("off", "0"):
        if value is None:
            return toolchains.data_dir() / "m2c"
        return None
    return Path(value)


def _context_dir(root: Path, digest: str) -> Path:
    return root / "contexts" / digest


def _context_dir_valid(final: Path, digest: str) -> bool:
    """The published context dir is complete and byte-identical to its identity."""
    try:
        done = json.loads((final / "DONE").read_text(encoding="utf-8"))
        if done.get("digest") != digest:
            return False
        if cache.sha((final / "context.c").read_bytes()) != done.get("text_sha256"):
            return False
        if cache.sha((final / "manifest.json").read_bytes()) != done.get("manifest_sha256"):
            return False
        return True
    except (OSError, ValueError):
        return False


_CTX_LOCKS: dict[str, threading.Lock] = {}
_CTX_LOCKS_GUARD = threading.Lock()


def _ctx_lock(digest: str) -> threading.Lock:
    with _CTX_LOCKS_GUARD:
        lock = _CTX_LOCKS.get(digest)
        if lock is None:
            lock = threading.Lock()
            _CTX_LOCKS[digest] = lock
            while len(_CTX_LOCKS) > 128:
                _CTX_LOCKS.pop(next(iter(_CTX_LOCKS)))
        return lock


def _publish_context(root: Path, digest: str, text: str, manifest: dict) -> Path:
    """Atomically publish the prepared context into its content-addressed directory. Publication is
    write-to-tmp + rename under a cross-process lease; dirs are keyed by content identity, so one
    invocation can never overwrite another revision's files. The files stay writable because m2c
    drops its own typemap cache (`context.c.m2c`) beside `context.c` — only this revision's dir."""
    final = _context_dir(root, digest)
    with _ctx_lock(digest), cache.dir_lease(final):
        if _context_dir_valid(final, digest):
            return final
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / "tmp" / f"ctx-{digest[:16]}-{os.getpid()}-{threading.get_ident()}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        text_bytes = text.encode("utf-8")
        manifest_bytes = cache.canonical(manifest)
        (tmp / "context.c").write_text(text, encoding="utf-8")
        (tmp / "manifest.json").write_bytes(manifest_bytes)
        (tmp / "DONE").write_text(json.dumps({
            "digest": digest, "text_sha256": cache.sha(text_bytes),
            "manifest_sha256": cache.sha(manifest_bytes),
        }, sort_keys=True), encoding="utf-8")
        try:
            os.replace(tmp, final)
        except OSError:
            if _context_dir_valid(final, digest):  # another invocation published first: use theirs
                shutil.rmtree(tmp, ignore_errors=True)
                return final
            shutil.rmtree(final, ignore_errors=True)  # a torn revision: replace it, it is the same identity
            os.replace(tmp, final)
        return final


def _stage_context_temp(digest: str, text: str, manifest: dict) -> Path:
    """A prepared context when no cache root is usable: still a real directory (m2c needs a file),
    just not reusable."""
    directory = Path(tempfile.mkdtemp(prefix="decompnet-m2c-ctx-"))
    (directory / "context.c").write_text(text, encoding="utf-8")
    (directory / "manifest.json").write_bytes(cache.canonical(manifest))
    (directory / "DONE").write_text(json.dumps({"digest": digest}, sort_keys=True), encoding="utf-8")
    return directory


_PREPARED_MEMO: dict[str, PreparedContext] = {}
_PREPARED_MEMO_LOCK = threading.Lock()
_PREPARED_MEMO_MAX = 128


# -----------------------------------------------------------------------------------
# P4.4 — DraftResult: fallback quality is visible
# -----------------------------------------------------------------------------------

@dataclass
class DraftResult:
    text: str | None = None
    outcome: str = "ok"                  # ok | error | unavailable | timeout
    provider: str = "m2c"
    target: str | None = None
    context_status: str = STATUS_OMITTED  # complete | degraded | omitted
    fallback: bool = False               # this text is the context-free retry
    compilation_ready: str = READINESS_UNKNOWN  # known | unknown
    unresolved: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)
    dependencies: dict = field(default_factory=dict)
    cached: bool = False

    def serialize(self) -> dict:
        """Everything except the text (the cache stores the text as its payload)."""
        out = {}
        for k, v in self.__dict__.items():
            out[k] = None if k == "text" else v
        return out

    @classmethod
    def from_json(cls, data: dict, text: str | None = None) -> "DraftResult":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        result = cls(text=text)
        for k, v in data.items():
            if k in known and k != "text":
                setattr(result, k, v)
        return result

    def annotated_text(self) -> str | None:
        """The draft with a bounded report header: what the engine should store/see (plan P4.4)."""
        if self.text is None:
            return None
        unresolved = ", ".join(f"{u['kind']}×{u['total']}" for u in self.unresolved) or "none"
        deps = self.dependencies
        m2c = (deps.get("m2c") or {}).get("version", "?")
        ctx = deps.get("context") or {}
        status = self.context_status
        if self.fallback:
            status += " (fallback: the prepared context was rejected or unavailable)"
        head = [
            "/* machine draft (m2c) — a starting point, not a match",
            f" * provider: {self.provider} {self.target or ''}".rstrip(),
            f" * context: {status}",
            f" * compilation readiness: {self.compilation_ready}",
            f" * unresolved placeholders: {unresolved}",
            f" * dependencies: m2c {m2c}, context {ctx.get('digest') or 'omitted'}, "
            f"draft key {deps.get('draft_key') or '-'}",
            " */",
        ]
        return "\n".join(head) + "\n" + self.text


_UNRESOLVED_PLACEHOLDERS = (
    ("unknown-type", re.compile(r"\bM2C_UNK(?:8|16|32)?\b")),
    ("unknown-field-access", re.compile(r"\bM2C_FIELD\b")),
    ("unresolved-bitwise", re.compile(r"\bM2C_BITWISE\b")),
    ("unresolved-carry", re.compile(r"\bM2C_CARRY\b")),
    ("unresolved-overflow", re.compile(r"\bM2C_OVERFLOW\b")),
    ("unresolved-error", re.compile(r"\bM2C_ERROR\b")),
)


def _audit_placeholders(text: str) -> list[dict]:
    """Every use of m2c's placeholder macros: the constructs whose semantics the draft guessed.
    Compilable syntax is not acceptance; these stay explicit (plan P4.4, D-08)."""
    rows = []
    for kind, pat in _UNRESOLVED_PLACEHOLDERS:
        found = list(pat.finditer(text))
        if found:
            first = min(found, key=lambda m: m.start())
            rows.append({"kind": kind, "macro": first.group(0), "total": len(found),
                         "line": text.count("\n", 0, first.start()) + 1})
    return rows


_LOCATION = re.compile(r"at line (\d+), column (\d+)")


def _m2c_failure_diag(out: str, err: str, stage: str) -> dict:
    text = (out or "") + "\n" + (err or "")
    m = _LOCATION.search(text)
    message = stage
    for marker in ("Syntax error when parsing C context", "Decompilation failure", "Internal error"):
        if marker in text:
            message += f": {marker}"
            break
    return _diag("error", message, "context.c" if "context" in stage else None,
                 int(m.group(1)) if m else None, int(m.group(2)) if m else None)


def _normalize_asm(asm_text: str, target: str) -> str:
    if target.startswith("gba"):  # luvdis / pret style: function macro, pre-UAL load mnemonics
        asm_text = re.sub(r"^\s*(?:non_word_aligned_)?thumb_func_start\s+(\w+)", r"glabel \1", asm_text,
                          flags=re.M)
        asm_text = re.sub(r"^\s*arm_func_start\s+(\w+)", r"glabel \1", asm_text, flags=re.M)
        asm_text = re.sub(r"\bld(s[hb])\b", r"ldr\1", asm_text)
    return asm_text


def _run_m2c_process(argv: list[str], timeout: float) -> tuple[int, str, str] | None:
    """One m2c invocation. None on timeout; rc=-2 when it cannot be started at all."""
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                             encoding="utf-8", errors="replace")
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return -2, "", ""


def _draft_key(asm_text: str, name: str, target: str, dialect: str, data_asm: str | None,
               valid: bool, options: dict, context_component: dict, m2c_version: str) -> str:
    return cache.sha(cache.canonical({
        "schema": DRAFT_KEY_SCHEMA, "asm": asm_text, "data_asm": data_asm, "name": name,
        "target": target, "dialect": dialect, "valid": valid, "options": options or {},
        "context": context_component, "m2c_version": m2c_version,
    }))


def draft(asm_text: str, name: str, toolchain_id: str | None = None, *,
          context_header: str = "", data_asm: str | None = None, timeout: float = 20,
          valid: bool = False, options: dict | None = None,
          cache_root: Path | str | None = None,
          defines: dict[str, str | None] | None = None,
          include_dirs: Sequence[str | Path] = (),
          dialect: str | None = None, target: str | None = None) -> DraftResult:
    """C draft for one function with full quality visibility (plan P4.4), cached content-addressed
    by target asm+data, context identity, m2c version, target mode and draft options (plan P4.3).
    `toolchain_id` selects the m2c target (M2C_TARGETS) and the legacy preprocessor; `target`/`dialect`
    override them. `valid`: m2c's compilable form (placeholders audited into `unresolved`).

    The context-free retry stays: when the context cannot be prepared or m2c rejects it, the draft
    is still produced without context, but labelled (`context_status == "omitted"`, `fallback`,
    diagnostics retained)."""
    target = target or M2C_TARGETS.get(toolchain_id or "")
    if not target:
        return DraftResult(outcome="unavailable", target=target,
                           diagnostics=[_diag("error", f"no m2c target for toolchain {toolchain_id!r}")])
    dialect = dialect or ("c++" if target.endswith("-c++") else "c")
    m2c_version = _m2c_version()
    asm_text = _normalize_asm(asm_text, target)

    prep: PreparedContext | None = None
    prep_diags: list[dict] = []
    context_component = {"status": STATUS_OMITTED, "reason": "no context provided"}
    if context_header.strip():
        try:
            prep = prepare_context(context_header, defines=defines, include_dirs=include_dirs,
                                   toolchain_id=toolchain_id, dialect=dialect, target=target,
                                   m2c_version=m2c_version, cache_root=cache_root)
            context_component = {"status": "prepared", "digest": prep.digest}
        except ContextPreparationError as e:
            context_component = {"status": STATUS_OMITTED, "reason": "preparation failed"}
            prep_diags = list(e.diagnostics)

    key = _draft_key(asm_text, name, target, dialect, data_asm, valid, options or {},
                     context_component, m2c_version)
    deps = {
        "draft_key": key,
        "m2c": {"version": m2c_version, "commit": (toolchains.installed("m2c") or {}).get("commit")},
        "target": target, "dialect": dialect, "valid": valid, "options": options or {},
        "context": {"digest": prep.digest if prep else None, "status": prep.status if prep else STATUS_OMITTED,
                    "manifest": prep.manifest if prep else None},
    }

    if not available():
        result = DraftResult(outcome="unavailable", target=target, dependencies=deps)
        result.diagnostics.append(_diag("error", "m2c is not installed"))
        return result

    root = None
    try:
        root = cache_root_for(cache_root)
    except OSError:
        root = None
    dcache = cache.cache_for_dir(root / "drafts") if root is not None else None
    if dcache is None:
        return _draft_uncached(asm_text, name, target, prep, prep_diags, data_asm, timeout, valid,
                               options or {}, deps)

    hit = dcache.get(key)
    if hit is not None:
        result = DraftResult.from_json(json.loads(hit.log)["result"], text=hit.payload.decode() if hit.payload else None)
        result.cached = True
        return result

    def produce() -> DraftResult:
        result = _draft_uncached(asm_text, name, target, prep, prep_diags, data_asm, timeout, valid,
                                 options or {}, deps)
        if result.outcome in ("ok", "error"):  # deterministic outcomes only, mirroring cache.py
            log = json.dumps({"result": result.serialize()})
            if result.outcome == "ok":
                dcache.put(key, ok=True, outcome=cache.OUTCOME_SUCCESS,
                           payload=(result.text or "").encode("utf-8"), log=log)
            else:
                dcache.put(key, ok=False, outcome=cache.OUTCOME_ERROR, payload=None, log=log)
        return result

    try:
        return dcache.get_or_run(key, produce, wait_timeout=max(timeout * 4, 120.0))
    except cache.CoalesceTimeout:
        return _draft_uncached(asm_text, name, target, prep, prep_diags, data_asm, timeout, valid,
                               options or {}, deps)


def _draft_uncached(asm_text: str, name: str, target: str, prep: PreparedContext | None,
                    prep_diags: list[dict], data_asm: str | None, timeout: float, valid: bool,
                    options: dict, deps: dict) -> DraftResult:
    """One real m2c run (plus the labelled context-free retry when a context was attempted)."""
    result = DraftResult(target=target, dependencies=deps)
    context_attempted = prep is not None or bool(prep_diags)
    if prep is not None:
        result.context_status = prep.status
    result.diagnostics = list(prep_diags)[:_MAX_DIAGNOSTICS]
    m2c = toolchains.venv_bin(toolchains.path("m2c") / "venv", "m2c")
    status = prep.status if prep is not None else STATUS_OMITTED
    warnings = [w for w in (prep.manifest["warnings"] if prep else [])][:_MAX_WARNINGS]
    with tempfile.TemporaryDirectory(prefix="decompnet-m2c-") as tmp:
        tdir = Path(tmp)
        (tdir / f"{name}.s").write_text(asm_text, encoding="utf-8")
        base = [str(m2c), *(["--valid-syntax"] if valid else []), "-t", target, "-f", name,
                str(tdir / f"{name}.s")]
        if data_asm:
            (tdir / "data.s").write_text(data_asm, encoding="utf-8")
            base.append(str(tdir / "data.s"))
        if prep is not None:
            ctx_argv = list(base)
            ctx_argv[1:1] = ["--context", str(prep.directory / "context.c")]
            run = _run_m2c_process(ctx_argv, timeout)
            if run is None:
                result.outcome = "timeout"
                result.diagnostics.append(_diag("error", "m2c timed out with the prepared context"))
                return result
            if run[0] == 0 and run[1].strip():
                return _finish_draft(result, run[1].strip(), status, False, warnings, valid, deps, prep)
            result.diagnostics.append(_m2c_failure_diag(run[1], run[2], "m2c rejected the prepared context"))
            result.warnings.append({"message": "the prepared context was rejected; retrying without it "
                                               "(a labelled, degraded fallback)", "file": "context.c"})
            result.fallback = True
            result.context_status = STATUS_OMITTED
        # context-free run: the only run, or the retry after a rejected/unavailable context
        run = _run_m2c_process(base, timeout)
        if run is None:
            result.outcome = "timeout"
            result.diagnostics.append(_diag("error", "m2c timed out"))
            return result
        rc, out, err = run
        if rc == 0 and out.strip():
            return _finish_draft(result, out.strip(), result.context_status,
                                 result.fallback or (context_attempted and prep is None),
                                 warnings, valid, deps, prep)
        if rc == -2:
            result.outcome = "error"
            result.diagnostics.append(_diag("error", f"could not start m2c ({m2c})"))
            return result
        result.outcome = "error"
        result.diagnostics.append(_m2c_failure_diag(out, err, "m2c failed"))
        return result


def _finish_draft(result: DraftResult, text: str, status: str, fallback: bool, warnings: list,
                  valid: bool, deps: dict, prep: PreparedContext | None) -> DraftResult:
    result.text = text
    result.outcome = "ok"
    result.context_status = status
    result.fallback = fallback
    result.warnings = list(warnings)[:_MAX_WARNINGS]
    result.compilation_ready = READINESS_KNOWN if deps.get("options", {}).get("compiled") else READINESS_UNKNOWN
    result.unresolved = _audit_placeholders(text) if valid else []
    result.dependencies["context"] = {
        "digest": prep.digest if prep else None,
        "status": prep.status if prep else STATUS_OMITTED,
        "manifest": prep.manifest if prep else None,
    }
    return result


# -----------------------------------------------------------------------------------
# Legacy text-only API (kept for existing callers; the engine can migrate to draft())
# -----------------------------------------------------------------------------------

def _context_for_m2c(header: str) -> str:
    """Legacy context shaper: strips preprocessor directive lines because m2c's parser does not run a
    preprocessor. Replaced by prepare_context() (which actually preprocesses, plan D06); kept only for
    callers that want the old lossy behaviour."""
    return "\n".join(line for line in header.splitlines() if not line.lstrip().startswith("#"))


def generate(fn: FunctionObj, toolchain_id: str | None, context_header: str = "", timeout: float = 20) -> str | None:
    """C draft for one function, or None when m2c or the target is unavailable or m2c fails."""
    return generate_from_asm(gnu_asm(fn), fn.name, toolchain_id, context_header, timeout=timeout)


def generate_from_asm(asm_text: str, name: str, toolchain_id: str | None, context_header: str = "",
                      data_asm: str | None = None, timeout: float = 20, valid: bool = False) -> str | None:
    """C draft from GNU/splat-style assembly text (plus optional data, for jump tables and strings). `valid`:
    m2c's compilable form (unknown types and expressions as M2C_ macros, see M2C_MACROS), for trying drafts as is."""
    return draft(asm_text, name, toolchain_id, context_header=context_header, data_asm=data_asm,
                 timeout=timeout, valid=valid).text


def run_m2c(asm_text: str, name: str, target: str, context_header: str = "", data_asm: str | None = None,
            timeout: float = 20, valid: bool = False) -> str | None:
    """Run m2c for one function with an explicit target triple (e.g. ppc-mwcc-c++)."""
    if not available():
        return None
    return draft(asm_text, name, None, target=target, context_header=context_header, data_asm=data_asm,
                 timeout=timeout, valid=valid).text
