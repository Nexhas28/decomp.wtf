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
import threading
from dataclasses import dataclass
from pathlib import Path

from . import cache as ccache
from . import wsl
from .limits import killed_for_memory

MAX_SOURCE_BYTES = 64 * 1024
# Per-compile resource caps (address space, CPU seconds, output file size) and a global
# concurrency cap, so a hostile submission can't take the host down with it.
LIMITS = ["--as=2147483648", "--cpu=30", "--fsize=67108864"]
_SLOTS = threading.BoundedSemaphore(max(2, (os.cpu_count() or 4) - 1))

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


class SourceRejected(ValueError):
    pass


def check_source(source: str) -> None:
    if len(source.encode()) > MAX_SOURCE_BYTES:
        raise SourceRejected(f"source too large (max {MAX_SOURCE_BYTES} bytes)")
    for inc in _INCLUDE.findall(source):
        if inc.startswith("/") or ".." in inc.split("/"):
            raise SourceRejected(f"include path not allowed: {inc}")
    code = _COMMENTS_AND_STRINGS.sub(" ", source)
    for pattern, why in _FORBIDDEN:
        if pattern.search(code):
            raise SourceRejected(why)


@dataclass
class CompilerConfig:
    cmd: list[str]
    timeout: float = 15.0
    sandbox: str = "auto"  # auto | bwrap | docker | none
    image: str | None = None  # for sandbox = "docker"
    ro_paths: tuple[str, ...] = ()
    include_dirs: tuple[str, ...] = ()  # absolute paths of extra (static) header dirs
    runtime32: str | None = None  # 32-bit x86 C runtime for old i386 compiler binaries (see toolchains)
    wsl: bool = False  # a Linux toolchain on Windows: runs inside WSL 2, `cmd` holds WSL paths (see wsl.py)
    cache: bool = True  # content-addressed compile cache (plan P2.4-P2.6); False disables read+write
    cache_dir: str | None = None  # cache location; default: DECOMPNET_CACHE_DIR or <tmp>/decompnet-cache


@dataclass
class CompileResult:
    ok: bool
    obj: bytes | None
    log: str


def _sandbox_mode(cfg: CompilerConfig) -> str:
    if cfg.wsl:
        return "wsl"
    if cfg.sandbox != "auto":
        return cfg.sandbox
    return "bwrap" if shutil.which("bwrap") else "none"


def _root(cfg: CompilerConfig, work: Path) -> str:
    """How the compiler sees the work folder (in WSL it works on a copy, see wsl.run)."""
    mode = _sandbox_mode(cfg)
    return "/work" if mode in ("bwrap", "docker") else wsl.stage_dir() if mode == "wsl" else str(work)


def _wrap(cfg: CompilerConfig, work: Path, argv: list[str], snapshot: Path | None = None) -> list[str]:
    mode = _sandbox_mode(cfg)
    if mode in ("none", "bwrap") and shutil.which("prlimit"):
        argv = ["prlimit", *LIMITS, "--", *argv]
    if mode == "none":
        return argv
    if mode == "docker":
        if not cfg.image:
            raise RuntimeError("sandbox = 'docker' needs compiler.image")
        return ["docker", "run", "--rm", "--network", "none", "--memory", "512m", "--pids-limit", "64",
                "-v", f"{work}:/work", "-w", "/work", cfg.image, *argv]
    if mode == "bwrap":
        # /tmp first, so read-only mounts of paths that live under /tmp are not hidden by it.
        cmd = ["bwrap", "--tmpfs", "/tmp", "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib64", "/lib64",
               "--symlink", "usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev"]
        if cfg.runtime32:
            # i386 binaries hardcode /lib/ld-linux.so.2; give them a private /lib with the pinned loader,
            # and point the loader at the pinned 32-bit libraries. 64-bit tools ignore those libraries.
            cmd += ["--dir", "/lib", "--ro-bind", f"{cfg.runtime32}/ld-linux.so.2", "/lib/ld-linux.so.2",
                    "--ro-bind", cfg.runtime32, "/opt/rt32", "--setenv", "LD_LIBRARY_PATH", "/opt/rt32"]
        else:
            cmd += ["--symlink", "usr/lib", "/lib"]
        for p in cfg.ro_paths:
            cmd += ["--ro-bind", p, p]
        cmd += ["--bind", str(work), "/work"]
        if snapshot is not None:
            # the shared immutable include snapshot, mounted read-only ON TOP of the writable
            # work bind (P2.7: no attempt can mutate another attempt's inputs) -- mounts apply
            # in command-line order, so this must come after the work bind.
            cmd += ["--ro-bind", str(snapshot), "/work/include"]
        cmd += ["--chdir", "/work", "--unshare-all", "--die-with-parent", "--new-session"]
        return [*cmd, "--", *argv]
    raise RuntimeError(f"unknown sandbox mode {mode!r}")


def _exec(cfg: CompilerConfig, work: Path, argv: list[str], timeout: float, cwd: Path | None = None, *,
          snapshot: Path | None = None, **stage):
    """`stage`: in WSL, run on a copy in WSL's own file system (wsl.run's outputs, inputs and stage)."""
    if _sandbox_mode(cfg) == "wsl":
        return wsl.run(argv, cwd or work, timeout, **stage)
    # in the work folder, never in the app's own: KMC GCC's assembler (binutils 2.6) can't read its input when the
    # current directory is / ("Bad file descriptor"), which it is for decomp.wtf started by a service or in Docker
    return subprocess.run(_wrap(cfg, work, argv, snapshot), cwd=cwd or work, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def run_in(cfg: CompilerConfig, work: Path, argv: list[str], timeout: float = 300, *,
           inputs: list[str] | None = None, outputs: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run a toolchain command inside `work` (the sandbox's /work), e.g. to build a whole exported project.
    Paths in `argv` must be relative to `work`. In WSL, naming the files it reads (`inputs`) and writes (`outputs`)
    lets old 32-bit compilers work on a copy in WSL's own file system, which they need (see wsl.run)."""
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

    cache = _cache_for(cfg)
    if cache is not None:
        key = ccache.build_key(cfg, context_header, source, mode=_sandbox_mode(cfg),
                               work_base=cache.root / "work")
        if key is not None:
            hit = cache.get(key.digest)
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
                # this waiter gave up on the shared flight: run the compile itself
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
    if mode != key.manifest["compiler"]["mode"] or mode not in ("bwrap", "none"):
        # the wrapper changed under us (or a mode the key builder never describes):
        # better an uncached compile than a mis-staged one
        raise ccache.CacheBypass("sandbox mode changed after the key was built")
    cache.pin_snapshot(key.dep_digest)  # in use: eviction must not delete it mid-attempt
    try:
        snap_inc = cache.ensure_snapshot(key)
        unit = ccache.UNIT_PROLOGUE + source + "\n"
        if mode == "bwrap":
            # the snapshot is mounted read-only at /work/include by _wrap; the work dir
            # only ever holds this attempt's writable files.
            with tempfile.TemporaryDirectory(prefix="decompnet-") as tmp:
                work = Path(tmp)
                (work / "func.c").write_text(unit, encoding="utf-8", newline="\n")
                root = _root(cfg, work)
                return _run_attempt(cfg, work, _argv(cfg, root), root, snap_inc)
        # sandbox "none": the compiler sees physical paths, so the work dir is the key's
        # deterministic path (same __FILE__ for the same key) under a cross-process lease.
        work = Path(cache.root) / "work" / key.work_name
        with ccache.dir_lease(work):
            _reset_work(work)
            _stage_include(snap_inc, work / "include")
            (work / "func.c").write_text(unit, encoding="utf-8", newline="\n")
            root = _root(cfg, work)
            return _run_attempt(cfg, work, _argv(cfg, root), root, None)
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


def _run_attempt(cfg: CompilerConfig, work: Path, argv: list[str], root: str,
                 snapshot: Path | None) -> _Attempt:
    try:
        with _SLOTS:
            proc = _exec(cfg, work, argv, cfg.timeout, snapshot=snapshot,
                         **({"outputs": ["func.o"], "stage": root} if _sandbox_mode(cfg) == "wsl" else {}))
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


def _compile_legacy(cfg: CompilerConfig, context_header: str, source: str) -> CompileResult:
    """The pre-cache path, byte-for-byte unchanged: a fresh temp tree per attempt, with
    every include directory copied in."""
    with tempfile.TemporaryDirectory(prefix="decompnet-") as tmp:
        work = Path(tmp)
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
            with _SLOTS:
                proc = _exec(cfg, work, argv, cfg.timeout,
                             **({"outputs": ["func.o"], "stage": root} if _sandbox_mode(cfg) == "wsl" else {}))
        except subprocess.TimeoutExpired:
            return CompileResult(False, None, f"compiler timed out after {cfg.timeout}s")
        log = (proc.stdout + proc.stderr).replace(root + "/", "")
        obj = work / "func.o"
        if proc.returncode != 0 or not obj.exists():
            return CompileResult(False, None, log[-4000:] or f"compiler exited with {proc.returncode}")
        return CompileResult(True, obj.read_bytes(), log[-2000:])
