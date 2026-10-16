"""Sandboxed compilation of untrusted agent-submitted C.

Agents never get to run their own compiler for anything that counts: every score and
every match is produced here, on the coordinator, with the project's pinned toolchain.

Compilation runs through the content-addressed compilation cache (plan P2.4-P2.6) with
immutable include snapshots (P2.7): source policy first, then a complete
`CompilationKey`, then a verified cache lookup, then single-flight coalesced execution.
When the key cannot be fully described -- or the cache is disabled -- the original
copy-per-attempt path below runs unchanged, so uncached behaviour is byte-identical to
what it was before the cache existed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import cache as ccache
from . import linuxbox, scheduler, telemetry
from .limits import killed_for_memory

MAX_SOURCE_BYTES = 64 * 1024
# The global concurrency cap is the 'compiler' pool of scheduler.BUDGET (see _slot), so a hostile submission can't
# take the computer down with it; the compiler container has its own memory and process limits (linuxbox).


@contextmanager
def _slot():
    """One unit of the global compiler budget around a compiler subprocess; the wait for it is the
    'compiler.queue' stage and the subprocess itself 'compiler.subprocess' (plan P0.3)."""
    with ExitStack() as stack:
        with telemetry.span("compiler.queue"):
            stack.enter_context(scheduler.BUDGET.slot("compiler"))
        with telemetry.span("compiler.subprocess"):
            yield


_COMMENTS_AND_STRINGS = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.S)
_FORBIDDEN = [
    (re.compile(r"\b(__)?asm(__)?\b"), "inline assembly is not allowed: matches must come from C"),
    (re.compile(r"\b(GLOBAL_ASM|INCLUDE_ASM|INCLUDE_RODATA|NON_MATCHING)\b"), "asm-include macros are not allowed"),
    (re.compile(r"\.incbin\b"), ".incbin is not allowed"),
    (re.compile(r"__attribute__\s*\(\(\s*(section|naked)\b"), "section/naked attributes are not allowed"),
    (re.compile(r"(#\s*pragma|\b_Pragma)\b"), "pragmas are not allowed (ask the project admin to whitelist one)"),
    (re.compile(r"#\s*include\s*<"), "system includes are not allowed; everything you need is in context.h"),
]
_INCLUDE = re.compile(r'#\s*include\s*"([^"]+)"')

# A register pin is a *declaration*, not a statement:
#     register u32 t __asm__("$4");
# It only constrains which register a variable lives in -- every instruction still comes from the
# compiler -- and it is the documented technique for register-allocation hard tails (BFM-decomp
# matching cookbook, section 10: "Closing the regalloc/scheduling hard tail by hand"). The pattern
# accepts no parentheses, braces or semicolons between `register` and the asm clause, so it cannot
# hide another statement, and the register name itself is restricted to a plain name or "$N".
# __asm__ *statements* (e.g. `__asm__ __volatile__("" : : "r"(x));`) remain forbidden.
_REGISTER_PIN = re.compile(
    r'\bregister\b[^;{}()]*?\b(?:__)?asm(?:__)?\s*\(\s*"(\$?[A-Za-z0-9_]+)"\s*\)\s*;'
)


class SourceRejected(ValueError):
    pass


def check_source(source: str) -> None:
    if len(source.encode()) > MAX_SOURCE_BYTES:
        raise SourceRejected(f"source too large (max {MAX_SOURCE_BYTES} bytes)")
    for inc in _INCLUDE.findall(source):
        if inc.startswith("/") or ".." in inc.split("/"):
            raise SourceRejected(f"include path not allowed: {inc}")
    # Remove register pins before masking comments/strings (the pin's own string is the register
    # name, which the masking below would otherwise hide from the pin pattern).
    pinned = _REGISTER_PIN.sub("register ", source)
    code = _COMMENTS_AND_STRINGS.sub(" ", pinned)
    for pattern, why in _FORBIDDEN:
        if pattern.search(code):
            raise SourceRejected(why)


@dataclass
class CompilerConfig:
    cmd: list[str]
    timeout: float = 15.0
    sandbox: str = "auto"  # auto (= none) | docker | none
    image: str | None = None  # for sandbox = "docker"
    ro_paths: tuple[str, ...] = ()
    include_dirs: tuple[str, ...] = ()  # absolute paths of extra (static) header dirs
    runtime32: str | None = None  # 32-bit x86 C runtime for old i386 compiler binaries (see toolchains)
    linux: bool = False  # a Linux-only toolchain, run in the compiler container (linuxbox)
    wsl: bool = False  # kept for configs written by older versions (see project.py: either flag means linux)
    cache: bool = True  # content-addressed compile cache (plan P2.4-P2.6); False disables read+write
    cache_dir: str | None = None  # cache location; default: DECOMPNET_CACHE_DIR or <tmp>/decompnet-cache
    # Optional two-stage variant: compile to assembly, run the named text pass, then assemble with
    # `asm_cmd`. Used for the PSY-Q assembler variant (see decompnet.rom.maspsx): the pinned GNU as 2.6
    # materialises gcc's blocking load-delay nop and expands `move` the PSY-Q way, while modern binutils
    # drops the nop but expands `move` as `or` - neither alone reproduces every ROM function, so the
    # variant is tried per attempt and only kept when it scores better.
    asm_cmd: tuple[str, ...] | None = None  # assembler argv template: {src} {obj} {include}
    asm_pass: str | None = None  # registered asm pass applied to the generated .s before assembly
    asm_pp_cmd: tuple[str, ...] | None = None  # assembly preprocessor: reads .s on stdin, writes .s on stdout


@dataclass
class CompileResult:
    ok: bool
    obj: bytes | None
    log: str


def _work_parent(cfg: CompilerConfig) -> str | None:
    """Where a compile's folder is made: in the compile area the compiler container shares, else temp."""
    if _linux(cfg):
        linuxbox.work_root().mkdir(parents=True, exist_ok=True)
        return str(linuxbox.work_root())
    return None


def _linux(cfg: CompilerConfig) -> bool:
    """A Linux-only toolchain: `linux` (current configs) or `wsl` (configs written before the rename)."""
    return bool(getattr(cfg, "linux", False) or getattr(cfg, "wsl", False))


def _sandbox_mode(cfg: CompilerConfig) -> str:
    if _linux(cfg):
        return "linux"
    return "none" if cfg.sandbox == "auto" else cfg.sandbox


def _root(cfg: CompilerConfig, work: Path) -> str:
    """How the compiler sees the work folder (in the compiler container it works on a copy, see linuxbox.script)."""
    mode = _sandbox_mode(cfg)
    return "/work" if mode == "docker" else linuxbox.stage_dir() if mode == "linux" else str(work)


def _wrap(cfg: CompilerConfig, work: Path, argv: list[str]) -> list[str]:
    mode = _sandbox_mode(cfg)
    if mode == "none":
        return argv
    if mode == "docker":
        if not cfg.image:
            raise RuntimeError("sandbox = 'docker' needs compiler.image")
        return ["docker", "run", "--rm", "--network", "none", "--memory", "512m", "--pids-limit", "64",
                "-v", f"{work}:/work", "-w", "/work", cfg.image, *argv]
    raise RuntimeError(f"unknown sandbox mode {mode!r}")


def _exec(cfg: CompilerConfig, work: Path, argv: list[str], timeout: float, cwd: Path | None = None, **stage):
    """`stage`: in the compiler container, run on a copy in its own file system (linuxbox.script's
    outputs/inputs/stage)."""
    if _sandbox_mode(cfg) == "linux":
        return linuxbox.run(argv, cwd or work, timeout, **stage)
    # in the work folder, never in the app's own: KMC GCC's assembler (binutils 2.6) can't read its input when the
    # current directory is / ("Bad file descriptor"), which it is for decomp.wtf started by a service
    return subprocess.run(_wrap(cfg, work, argv), cwd=cwd or work, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def run_in(cfg: CompilerConfig, work: Path, argv: list[str], timeout: float = 300, *,
           inputs: list[str] | None = None, outputs: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run a toolchain command inside `work` (the sandbox's /work), e.g. to build a whole exported project.
    Paths in `argv` must be relative to `work`. In the compiler container, naming the files it reads (`inputs`) and
    writes (`outputs`) lets old 32-bit compilers work on a copy in the container's own file system, which they need."""
    return _exec(cfg, Path(work), argv, timeout, cwd=Path(work),
                 **({} if outputs is None else {"inputs": inputs, "outputs": outputs}))


def compile_source(cfg: CompilerConfig, context_header: str, source: str, trusted: bool = False) -> CompileResult:
    # Source policy runs BEFORE any cache lookup: cache possession is not authorisation,
    # a rejected source must never obtain a cached object (plan P2.5).
    if not trusted:
        try:
            check_source(source)
        except SourceRejected as e:
            return CompileResult(False, None, f"rejected: {e}")

    if cfg.asm_cmd is not None:
        # variant compiles are attempt-scoped and few: run them directly, never through the cache
        return _compile_variant(cfg, context_header, source)

    cache = _cache_for(cfg)
    if cache is not None:
        key = ccache.build_key(cfg, context_header, source, mode=_sandbox_mode(cfg),
                               work_base=cache.root / "work")
        if key is not None:
            hit = cache.get(key.digest, request=key.request_id)
            if hit is not None:
                return CompileResult(hit.ok, hit.payload, hit.log)
            transient = cache.transient_get(key.digest)
            if transient is not None:
                return transient
            try:
                return cache.get_or_run(key.digest,
                                        producer=lambda: _produce(cache, cfg, context_header, source, key),
                                        wait_timeout=max(cfg.timeout * 4, 120.0))
            except ccache.CoalesceTimeout:
                if cache.in_flight(key.digest):
                    # the identical compile is still running: never start a second one (P2.6);
                    # this timeout is not cached and not a deterministic failure
                    return CompileResult(False, None, "timeout: waiting for an identical in-flight compile")
                # the shared flight ended without a result for this waiter: run the compile itself
                try:
                    return _produce(cache, cfg, context_header, source, key)
                except ccache.CoalesceTimeout:
                    return _compile_legacy(cfg, context_header, source)
    return _compile_legacy(cfg, context_header, source)


def _cache_for(cfg: CompilerConfig) -> ccache.CompileCache | None:
    if not cfg.cache or os.environ.get("DECOMPNET_COMPILE_CACHE") == "0":
        return None
    if cfg.cache_dir:
        root = Path(cfg.cache_dir)
    elif os.environ.get("DECOMPNET_CACHE_DIR"):
        root = Path(os.environ["DECOMPNET_CACHE_DIR"])
    else:
        root = Path(tempfile.gettempdir()) / "decompnet-cache"
    return ccache.cache_for_dir(root)


@dataclass
class _Attempt:
    public: CompileResult
    outcome: str  # ccache.OUTCOME_SUCCESS | ccache.OUTCOME_ERROR | a transient kind
    payload: bytes | None


def _produce(cache: ccache.CompileCache, cfg: CompilerConfig, context_header: str, source: str,
             key: ccache.CompilationKey) -> CompileResult:
    """One cacheable compile run: execute, classify the outcome, publish (or back off)."""
    try:
        attempt = _compile_cached(cfg, context_header, source, key, cache)
    except ccache.CacheBypass:
        return _compile_legacy(cfg, context_header, source)
    try:
        if attempt.outcome == ccache.OUTCOME_SUCCESS:
            cache.put(key.digest, ok=True, outcome=ccache.OUTCOME_SUCCESS,
                      payload=attempt.payload, log=attempt.public.log, manifest=key.manifest)
        elif attempt.outcome == ccache.OUTCOME_ERROR:
            cache.put(key.digest, ok=False, outcome=ccache.OUTCOME_ERROR,
                      payload=None, log=attempt.public.log, manifest=key.manifest)
        else:
            cache.record_transient(key.digest, attempt.public)
    except (OSError, ValueError):
        pass  # the cache is best-effort: never fail a compile because of it
    return attempt.public


def _argv(cfg: CompilerConfig, root: str) -> list[str]:
    # mirrors cache.build_key's substitution: the argv the compiler actually runs
    subst = {"{src}": f"{root}/func.c", "{obj}": f"{root}/func.o", "{include}": f"{root}/include"}
    argv = [subst.get(a, a) for a in cfg.cmd]
    return [a.replace("{include}", subst["{include}"]) for a in argv]


def _stage_include(snap_inc: Path, work_inc: Path) -> None:
    """Per-attempt include tree. Never hard-links the shared snapshot: a copy (read-only
    permissions preserved) means one attempt mutating its include file cannot touch
    another attempt's inputs (plan P2.7.4)."""
    shutil.copytree(snap_inc, work_inc, symlinks=True)


def _compile_cached(cfg: CompilerConfig, context_header: str, source: str,
                    key: ccache.CompilationKey, cache: ccache.CompileCache) -> _Attempt:
    """Run the compiler against the key's immutable include snapshot; writable outputs
    stay in a per-attempt directory."""
    mode = _sandbox_mode(cfg)
    if mode != key.manifest["compiler"]["mode"] or mode != "none":
        # the wrapper changed under us (or a mode the key builder never describes):
        # better an uncached compile than a mis-staged one
        raise ccache.CacheBypass("sandbox mode changed after the key was built")
    cache.pin_snapshot(key.dep_digest)  # in use: eviction must not delete it mid-attempt
    try:
        snap_inc = cache.ensure_snapshot(key)
        unit = ccache.UNIT_PROLOGUE + source + "\n"
        # sandbox "none": the compiler sees physical paths, so the work dir is the key's
        # deterministic path (same __FILE__ for the same key) under a cross-process lease.
        work = Path(cache.root) / "work" / key.work_name
        with ccache.dir_lease(work):
            _reset_work(work)
            _stage_include(snap_inc, work / "include")
            (work / "func.c").write_text(unit, encoding="utf-8", newline="\n")
            root = _root(cfg, work)
            return _run_attempt(cfg, work, _argv(cfg, root), root)
    finally:
        cache.unpin_snapshot(key.dep_digest)


def _reset_work(work: Path) -> None:
    """Recreate this attempt's staging dir. The previous run left read-only files and
    directories in it, so restore write permissions before removing."""
    if work.exists():
        for dirpath, dirnames, filenames in os.walk(work):
            for name in filenames:
                try:
                    os.chmod(Path(dirpath) / name, 0o644)
                except OSError:
                    pass
            try:
                os.chmod(dirpath, 0o755)
            except OSError:
                pass
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)


def _run_attempt(cfg: CompilerConfig, work: Path, argv: list[str], root: str) -> _Attempt:
    try:
        with _slot():
            proc = _exec(cfg, work, argv, cfg.timeout,
                         **({"outputs": ["func.o"], "stage": root} if _sandbox_mode(cfg) == "linux" else {}))
    except subprocess.TimeoutExpired:
        return _Attempt(CompileResult(False, None, f"compiler timed out after {cfg.timeout}s"), "timeout", None)
    except FileNotFoundError as e:
        # a missing compiler/wrapper is infrastructure, not a compile result: transient
        return _Attempt(CompileResult(False, None, f"compiler not found: {e}"), "missing-tool", None)
    except OSError as e:
        return _Attempt(CompileResult(False, None, f"compile failed: {e}"), "io", None)
    log = (proc.stdout + proc.stderr).replace(root + "/", "")
    obj = work / "func.o"
    if proc.returncode != 0:
        if proc.returncode < 0 or killed_for_memory(proc.returncode):
            # killed by a signal / OOM: infrastructure, transient -- never cached (P2.5)
            return _Attempt(CompileResult(False, None, log[-4000:] or f"compiler exited with {proc.returncode}"),
                            "killed", None)
        # a deterministic compile error: the cacheable negative under the complete key
        return _Attempt(CompileResult(False, None, log[-4000:] or f"compiler exited with {proc.returncode}"),
                        ccache.OUTCOME_ERROR, None)
    if not obj.exists():
        # success exit but no output: suspicious, treat as transient rather than a cached negative
        return _Attempt(CompileResult(False, None, log[-4000:] or f"compiler exited with {proc.returncode}"),
                        "no-output", None)
    payload = obj.read_bytes()
    return _Attempt(CompileResult(True, payload, log[-2000:]), ccache.OUTCOME_SUCCESS, payload)


def _compile_variant(cfg: CompilerConfig, context_header: str, source: str) -> CompileResult:
    """Two-stage compile for an assembler variant: `gcc -S`, the named asm pass, then `cfg.asm_cmd`.

    Staging matches _compile_legacy (fresh temp tree, include copy, context.h), so the only difference
    from a normal compile is which assembler runs and what text it is handed."""
    from . import toolchains

    with tempfile.TemporaryDirectory(prefix="decompnet-var-", dir=_work_parent(cfg)) as tmp:
        work = Path(tmp)
        if _linux(cfg):
            work.chmod(0o777)  # the container's user isn't the Mac user
        inc = work / "include"
        inc.mkdir()
        for d in cfg.include_dirs:
            shutil.copytree(d, inc, dirs_exist_ok=True)
        (inc / "context.h").write_text(context_header, encoding="utf-8", newline="\n")
        (work / "func.c").write_text('#include "context.h"\n#line 1 "func.c"\n' + source + "\n",
                                     encoding="utf-8", newline="\n")

        root = _root(cfg, work)
        subst = {"{src}": f"{root}/func.c", "{asm}": f"{root}/func.s", "{obj}": f"{root}/func.o",
                 "{include}": f"{root}/include"}
        # `-S` instead of `-c`, and the object path element becomes the assembly path: passing two -o
        # flags (or letting gcc write assembly into func.o) makes the stage fail on old GCCs.
        compile_argv: list[str] = []
        saw_dash_s = False
        for a in cfg.cmd:
            if a == "-c":
                a = "-S"
            if a == "{obj}":
                a = "{asm}"
            if a == "-S":
                saw_dash_s = True
            compile_argv.append(subst.get(a, a))
        compile_argv = [a.replace("{include}", subst["{include}"]) for a in compile_argv]
        if not saw_dash_s:
            compile_argv.insert(1, "-S")
        if subst["{asm}"] not in compile_argv:  # placeholders are substituted already: look for the path
            compile_argv += ["-o", subst["{asm}"]]
        staged = {"outputs": ["func.s", "func.o"], "stage": root} if _sandbox_mode(cfg) == "linux" else {}
        try:
            with _slot():
                proc = _exec(cfg, work, compile_argv, cfg.timeout, **staged)
        except subprocess.TimeoutExpired:
            return CompileResult(False, None, f"compiler timed out after {cfg.timeout}s")
        log = (proc.stdout + proc.stderr).replace(root + "/", "")
        asm_file = work / "func.s"
        if proc.returncode != 0 or not asm_file.exists():
            return CompileResult(False, None, log[-4000:] or f"compiler exited with {proc.returncode}")

        asm_text = asm_file.read_text(encoding="utf-8", errors="replace")
        if cfg.asm_pp_cmd:
            # the PSY-Q assembly preprocessing stage (maspsx): jump tables, %hi/%lo of local labels and
            # other gcc output that the bare assembler cannot encode. stdin -> stdout, same text domain.
            try:
                pp = subprocess.run(list(cfg.asm_pp_cmd), input=asm_text, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=cfg.timeout, cwd=work)
            except subprocess.TimeoutExpired:
                return CompileResult(False, None, f"assembler preprocessor timed out after {cfg.timeout}s")
            log += (pp.stderr or "").replace(root + "/", "")
            if pp.returncode != 0 or not pp.stdout.strip():
                return CompileResult(False, None, (pp.stderr or "assembler preprocessor failed")[-4000:])
            asm_text = pp.stdout
        if cfg.asm_pass:
            try:
                asm_text = toolchains.apply_asm_pass(cfg.asm_pass, asm_text)
            except ValueError as e:
                return CompileResult(False, None, f"asm pass failed: {e}")
        if cfg.asm_pass or cfg.asm_pp_cmd:
            asm_file.write_text(asm_text, encoding="utf-8", newline="\n")

        asm_subst = {"{src}": f"{root}/func.s", "{asm}": f"{root}/func.s", "{obj}": f"{root}/func.o",
                     "{include}": f"{root}/include"}
        asm_argv = [asm_subst.get(a, a) for a in cfg.asm_cmd]
        asm_argv = [a.replace("{include}", asm_subst["{include}"]) for a in asm_argv]
        try:
            with _slot():
                proc = _exec(cfg, work, asm_argv, cfg.timeout, **staged)
        except subprocess.TimeoutExpired:
            return CompileResult(False, None, f"assembler timed out after {cfg.timeout}s")
        log += (proc.stdout + proc.stderr).replace(root + "/", "")
        obj = work / "func.o"
        if proc.returncode != 0 or not obj.exists():
            return CompileResult(False, None, log[-4000:] or f"assembler exited with {proc.returncode}")
        return CompileResult(True, obj.read_bytes(), log[-2000:])


def _compile_legacy(cfg: CompilerConfig, context_header: str, source: str) -> CompileResult:
    """The pre-cache path, byte-for-byte unchanged: a fresh temp tree per attempt, with
    every include directory copied in."""
    with tempfile.TemporaryDirectory(prefix="decompnet-", dir=_work_parent(cfg)) as tmp:
        work = Path(tmp)
        if _linux(cfg):
            work.chmod(0o777)  # the container's user isn't the Mac user
        inc = work / "include"
        inc.mkdir()
        for d in cfg.include_dirs:
            shutil.copytree(d, inc, dirs_exist_ok=True)
        # LF on every platform: to the old GCCs a backslash before CRLF doesn't continue a line ("stray '\' in program")
        (inc / "context.h").write_text(context_header, encoding="utf-8", newline="\n")
        (work / "func.c").write_text('#include "context.h"\n#line 1 "func.c"\n' + source + "\n", encoding="utf-8",
                                     newline="\n")

        root = _root(cfg, work)
        subst = {"{src}": f"{root}/func.c", "{obj}": f"{root}/func.o", "{include}": f"{root}/include"}
        argv = [subst.get(a, a) for a in cfg.cmd]
        argv = [a.replace("{include}", subst["{include}"]) for a in argv]
        try:
            with _slot():
                proc = _exec(cfg, work, argv, cfg.timeout,
                             **({"outputs": ["func.o"], "stage": root} if _sandbox_mode(cfg) == "linux" else {}))
        except subprocess.TimeoutExpired:
            return CompileResult(False, None, f"compiler timed out after {cfg.timeout}s")
        log = (proc.stdout + proc.stderr).replace(root + "/", "")
        obj = work / "func.o"
        if proc.returncode != 0 or not obj.exists():
            return CompileResult(False, None, log[-4000:] or f"compiler exited with {proc.returncode}")
        return CompileResult(True, obj.read_bytes(), log[-2000:])
