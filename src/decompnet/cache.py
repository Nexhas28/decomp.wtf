"""Content-addressed compilation cache (plan P2.4-P2.6, P2.8) and immutable include
snapshots (P2.7).

Design boundaries, all deliberate:

* A `CompilationKey` is built from a canonical manifest, never from ad-hoc string
  concatenation: the exact generated translation-unit bytes, the exact context header,
  per-directory include digests *in order* (later directories shadow earlier ones on a
  name clash, matching the legacy copytree), the ordered argv after placeholder
  substitution, the resolved compiler executable and its digest, wrapper identity
  (sandbox mode, prlimit/bwrap binaries), read-only path digests, a fixed set of
  toolchain-relevant environment variables, and the path semantics the compiler sees.
  When any of these cannot be fully described, `build_key` returns None and the caller
  bypasses the cache (P2.4). WSL and docker modes always bypass: WSL stages into a fresh
  per-attempt directory and a docker image's contents cannot be described from here.
* Only "success" and deterministic "error" outcomes are stored. Killed processes,
  missing tools, timeouts, cancellations and IO failures are transient: they are never
  published and get a short in-process retry backoff (P2.5). A negative result is an
  explicit entry, never an empty object masquerading as success.
* Publication is write-to-temp + atomic rename; payloads are hash-verified on every
  read; corrupt entries are quarantined, never served, and recomputed (P2.4).
* Single-flight coalescing is in-process (this coordinator is a single process,
  P2.6.6): one producer per identical key, waiters attach, a waiter's timeout detaches
  only that waiter, and when the last consumer detaches the producer gets a best-effort
  cancel callback. Cross-process safety comes from atomic publication (last writer wins
  for an identical key); cross-process in-flight deduplication is explicitly out of
  scope.
* Include snapshots are content-addressed directories built once per dependency revision
  (header + include dirs, in order), verified against the key's digests before
  publication, staged read-only, and reused by every attempt. Attempts never hard-link
  writable shared inputs: sandboxed modes mount the snapshot read-only and unsandboxed
  modes copy it per attempt, so one attempt cannot mutate another's inputs (P2.7).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import shutil
import stat as stat_module
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from . import telemetry

try:
    import fcntl
except ImportError:  # Windows: no cross-process directory leases (documented)
    fcntl = None

CACHE_SCHEMA = 1
# The exact bytes prepended to the submitted source before compiling (compiler.py writes
# UNIT_PROLOGUE + source + "\n"). Changing the prologue changes the unit bytes and thus
# every key; CACHE_SCHEMA bumps are the backstop for generator changes.
UNIT_PROLOGUE = '#include "context.h"\n#line 1 "func.c"\n'

DEFAULT_MAX_BYTES = 512 << 20
DEFAULT_MAX_AGE = 14 * 86400.0
DEFAULT_TRANSIENT_RETRY = 2.0
REQUESTS_MAX = 4096  # remembered request identities, for miss-reason classification
STALE_PUT_AGE = 3600.0  # publish temp files older than this belong to no live writer

# Environment variables that can change what a toolchain produces or how it resolves
# inputs. Kept short on purpose: everything else is covered by explicit manifest
# components (resolved executable, argv, ro_paths).
KEY_ENV = ("GCC_EXEC_PREFIX", "COMPILER_PATH", "LIBRARY_PATH", "CPATH",
           "C_INCLUDE_PATH", "OBJC_INCLUDE_PATH", "LANG", "LC_ALL")

OUTCOME_SUCCESS = "success"
OUTCOME_ERROR = "error"  # deterministic compiler error: cacheable negative
TRANSIENT = ("timeout", "killed", "missing-tool", "cancelled", "io", "no-output")


class CoalesceTimeout(TimeoutError):
    pass


class CacheBypass(Exception):
    """The cache cannot serve this request; fall back to the uncached path."""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


# -----------------------------------------------------------------------------------
# Content-addressed tree digests (include dirs, ro_paths, toolchain directories).
#
# The digest covers (relative path -> kind/link-target/content) facts only, never
# timestamps. A cheap stat signature (sizes + mtimes) memoises the expensive hashing,
# so repeated key builds re-walk but only re-hash when something actually changed.
# -----------------------------------------------------------------------------------

_TREE_MEMO: dict[Path, tuple[tuple, tuple, tuple]] = {}  # path -> (stat signature, facts, files)
_TREE_MEMO_LOCK = threading.Lock()
_TREE_MEMO_MAX = 128


@dataclass(frozen=True)
class _TreeFacts:
    entries: tuple  # ((rel, kind, size, mtime_ns, link_target, content_sha), ...)
    files: tuple    # ((rel, kind, content_sha), ...) content-addressed only


def tree_facts(root: Path) -> _TreeFacts | None:
    """Describe a directory tree for key building. None when the tree cannot be fully
    described (unreadable entries, sockets/fifos, ...) -- the caller then bypasses."""
    root = Path(root).resolve()
    if not root.is_dir():
        return None
    walked = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            filenames.sort()
            rel = Path(dirpath).relative_to(root)
            for name in filenames:
                p = Path(dirpath) / name
                r = name if str(rel) == "." else str(rel / name)
                try:
                    st = os.lstat(p)
                except OSError:
                    return None
                if stat_module.S_ISLNK(st.st_mode):
                    try:
                        target = os.readlink(p)
                        fst = os.stat(p)  # follows the link
                    except OSError:
                        return None
                    if stat_module.S_ISREG(fst.st_mode):
                        try:
                            content = sha(p.read_bytes())
                        except OSError:
                            return None
                    else:
                        content = ""  # symlink to a directory: its contents are walked
                    walked.append((r, "l", st.st_size, st.st_mtime_ns, target, content))
                elif stat_module.S_ISREG(st.st_mode):
                    try:
                        content = sha(p.read_bytes())
                    except OSError:
                        return None
                    walked.append((r, "f", st.st_size, st.st_mtime_ns, "", content))
                else:
                    return None  # sockets/fifos/devices cannot be described or copied
    except OSError:
        return None
    sig = tuple((r, k, s, m, t) for r, k, s, m, t, _ in sorted(walked))
    with _TREE_MEMO_LOCK:
        hit = _TREE_MEMO.get(root)
        if hit and hit[0] == sig:
            return _TreeFacts(hit[1], hit[2])
        facts = tuple(sorted(walked))
        files = tuple((r, k, c) for r, k, *_rest, t, c in sorted(walked))
        # walk entries are (rel, kind, size, mtime, target, content) -- drop nothing
        _TREE_MEMO[root] = (sig, facts, files)
        while len(_TREE_MEMO) > _TREE_MEMO_MAX:
            _TREE_MEMO.pop(next(iter(_TREE_MEMO)))
        return _TreeFacts(facts, files)


def tree_digest(root: Path) -> str | None:
    """Content-addressed digest of a directory (or None when undescribable)."""
    if Path(root).is_dir():
        facts = tree_facts(root)
        return None if facts is None else sha(canonical(facts.files))
    return file_sha(Path(root))


def file_sha(path: Path) -> str | None:
    try:
        return sha(Path(path).read_bytes())
    except OSError:
        return None


# -----------------------------------------------------------------------------------
# CompilationKey
# -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class CompilationKey:
    digest: str          # sha256 of the canonical manifest
    manifest: dict       # full canonical manifest (diagnostics / toolchain manifest)
    dep_digest: str      # include-snapshot identity: cache/snapshots/<dep_digest>
    work_name: str | None  # deterministic staging dir name for sandbox "none"

    @property
    def dependencies(self) -> dict:
        return self.manifest["dependencies"]

    @property
    def request_id(self) -> str:
        return request_identity(self.manifest)


def request_identity(manifest: dict) -> str:
    """Dependency-independent identity of "what was asked": the exact unit bytes plus the
    toolchain invocation (argv with the staging root masked, resolved executable and its
    digest, wrapper mode). The same request under changed include dirs, context header,
    read-only paths or environment keeps this identity while its full key changes, which
    is how a miss is told apart as "dependency changed" (P2.8)."""
    comp = manifest["compiler"]
    root = manifest["path_semantics"]["root"]
    argv = [a.replace(root, "<root>") for a in comp["argv"]]
    return sha(canonical({"unit": manifest["unit"], "argv": argv, "exe": comp["exe"],
                          "exe_sha": comp["exe_sha"], "mode": comp["mode"]}))


def _mini_mode(cfg) -> str:
    """Mirror of compiler._sandbox_mode, so build_key works without importing compiler."""
    if cfg.wsl:
        return "wsl"
    if cfg.sandbox != "auto":
        return cfg.sandbox
    return "bwrap" if shutil.which("bwrap") else "none"


def _bin_component(path: str | None) -> dict | None:
    if path is None:
        return None
    try:
        return {"path": path, "sha": sha(Path(path).read_bytes())}
    except OSError:
        return None


def build_key(cfg, context_header: str, source: str, *, mode: str | None = None,
              work_base: Path | None = None) -> CompilationKey | None:
    """Complete compilation identity, or None when the wrapper cannot fully describe its
    inputs (the caller then bypasses the cache, plan P2.4)."""
    if mode is None:
        mode = _mini_mode(cfg)
    if mode in ("wsl", "docker"):
        # wsl: a fresh random stage dir per attempt (path semantics not describable);
        # docker: image contents cannot be described from here.
        return None

    # -- exact source / unit bytes (never whitespace-folded) -----------------------
    source_bytes = source.encode("utf-8")
    unit_bytes = UNIT_PROLOGUE.encode("utf-8") + source_bytes + b"\n"
    header_bytes = context_header.encode("utf-8")

    # -- include directories, in order; later dirs shadow earlier ones, like copytree
    includes: list[dict] = []
    merged: dict[str, tuple] = {}
    for d in cfg.include_dirs:
        facts = tree_facts(Path(d))
        if facts is None:
            return None
        includes.append({"path": str(Path(d).resolve()), "digest": sha(canonical(facts.files))})
        merged.update((r, (k, c)) for r, k, c in facts.files)
    merged["context.h"] = ("f", sha(header_bytes))  # written last: wins over any copy
    tree = sha(canonical(sorted((r, k, c) for r, (k, c) in merged.items())))
    deps = {"header": base64.b64encode(header_bytes).decode(), "dirs": includes, "tree": tree}
    dep_digest = sha(canonical(deps))

    # -- toolchain identity --------------------------------------------------------
    if not cfg.cmd:
        return None
    exe = cfg.cmd[0]
    exe_path = exe if os.path.isabs(exe) else shutil.which(exe)
    if not exe_path or not os.path.exists(exe_path):
        return None
    exe_sha = file_sha(Path(exe_path))
    if exe_sha is None:
        return None

    ro_paths: list[dict] = []
    for p in cfg.ro_paths:
        digest = tree_digest(Path(p))
        if digest is None:
            return None
        ro_paths.append({"path": str(Path(p).resolve()), "digest": digest})

    runtime32 = None
    if cfg.runtime32:
        runtime32 = tree_digest(Path(cfg.runtime32))
        if runtime32 is None:
            return None

    prlimit = shutil.which("prlimit") if mode in ("none", "bwrap") else None
    bwrap = shutil.which("bwrap") if mode == "bwrap" else None
    pr, bw = _bin_component(prlimit), _bin_component(bwrap)
    if mode == "bwrap" and bw is None:
        return None  # wrapper identity not describable

    def make_manifest(root_str: str) -> dict:
        subst = {"{src}": f"{root_str}/func.c", "{obj}": f"{root_str}/func.o",
                 "{include}": f"{root_str}/include"}
        argv = [subst.get(a, a) for a in cfg.cmd]
        argv = [a.replace("{include}", subst["{include}"]) for a in argv]
        return {
            "cache_schema": CACHE_SCHEMA,
            "unit": base64.b64encode(unit_bytes).decode(),
            "dependencies": deps,
            "compiler": {
                "argv": argv,
                "exe": exe_path, "exe_sha": exe_sha,
                "mode": mode, "sandbox": cfg.sandbox, "timeout": cfg.timeout,
                "prlimit": pr, "bwrap": bw, "image": cfg.image,
                "ro_paths": ro_paths, "runtime32": runtime32,
                "env": {k: os.environ.get(k) for k in KEY_ENV},
            },
            "path_semantics": {"root": root_str, "work_name": None, "staging": "virtual"},
        }

    work_name = None
    if mode == "none":
        # Sandbox "none" exposes the physical work path to the compiler (__FILE__,
        # .file directives), so it must be identical for identical keys: stage at a
        # key-derived path under the cache root.
        if work_base is None:
            return None
        work_base = Path(work_base).resolve()  # the runtime stage path is resolved
        pre = sha(canonical(make_manifest("WORK")))
        work_name = f"decompnet-work-{pre[:24]}"
        root = str(Path(work_base) / work_name)
        manifest = make_manifest(root)
        manifest["path_semantics"] = {"root": root, "work_name": work_name,
                                      "staging": "deterministic"}
    else:  # bwrap: the compiler only ever sees /work
        manifest = make_manifest("/work")

    digest = sha(canonical(manifest))
    return CompilationKey(digest, manifest, dep_digest, work_name)


# -----------------------------------------------------------------------------------
# Immutable include snapshots (P2.7)
# -----------------------------------------------------------------------------------

def _make_readonly(dir_path: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(dir_path):
        dirnames.sort()
        for name in filenames:
            try:
                os.chmod(Path(dirpath) / name, 0o444)
            except OSError:
                pass
        try:
            os.chmod(dirpath, 0o555)
        except OSError:
            pass


def _tree_bytes(root: Path) -> int:
    """Approximate on-disk size of a directory tree (file bytes plus a per-tree constant),
    used to count include snapshots against the size quota."""
    total = 512
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.lstat(Path(dirpath) / name).st_size
            except OSError:
                pass
    return total


def _remove_tree(path: Path) -> bool:
    """Delete a read-only snapshot tree (its directories are 0o555, so they are made
    writable first)."""
    for dirpath, _dirnames, _filenames in os.walk(path):
        try:
            os.chmod(dirpath, 0o755)
        except OSError:
            pass
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


@contextlib.contextmanager
def dir_lease(work: Path, timeout: float = 120.0) -> Iterator[None]:
    """Cross-process lease on a deterministic staging dir. flock auto-releases on process
    death, so no owner/expiry bookkeeping is needed (P2.6). On platforms without fcntl
    (Windows) there is no cross-process lock; in-process single-flight still applies."""
    if fcntl is None:
        yield
        return
    work.parent.mkdir(parents=True, exist_ok=True)
    lockfile = work.parent / (work.name + ".lock")
    fd = os.open(lockfile, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise CoalesceTimeout(f"staging dir {work} is busy")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# -----------------------------------------------------------------------------------
# The cache itself
# -----------------------------------------------------------------------------------

@dataclass
class Entry:
    key: str
    ok: bool
    outcome: str
    payload: bytes | None
    log: str
    created: float = 0.0


class _Flight:
    def __init__(self, key: str, producer: Callable[[], Any], cancel: Callable[[], Any] | None,
                 cond: threading.Condition):
        self.key = key
        self.producer = producer
        self.cancel = cancel
        self.cond = cond
        self.done = False
        self.exc: BaseException | None = None
        self.value: Any = None
        self.consumers = 0

    def run(self) -> None:
        try:
            value = self.producer()
        except BaseException as e:
            value, self.exc = None, e
        with self.cond:
            self.value, self.done = value, True
            self.cond.notify_all()

    def wait(self, timeout: float | None) -> Any:
        with self.cond:
            deadline = None if timeout is None else time.monotonic() + timeout
            while not self.done:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise CoalesceTimeout(f"timed out waiting for compile of {self.key}")
                self.cond.wait(remaining)
            if self.exc is not None:
                raise self.exc
            return self.value


class CompileCache:
    """Content-addressed object store with atomic publication, verified reads,
    quarantine, quotas, and in-process single-flight coalescing."""

    def __init__(self, root: Path | str, *, max_bytes: int | None = DEFAULT_MAX_BYTES,
                 max_age: float | None = DEFAULT_MAX_AGE, max_entries: int | None = None,
                 transient_retry: float = DEFAULT_TRANSIENT_RETRY):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.max_age = max_age
        self.max_entries = max_entries
        self.transient_retry = transient_retry
        self._lock = threading.RLock()
        self._flights: dict[str, _Flight] = {}
        self._pinned: dict[str, int] = {}
        self._pinned_snaps: dict[str, int] = {}
        self._transient: dict[str, tuple[float, Any]] = {}
        self._approx_bytes: float | None = None
        self._requests: dict[str, str] = {}  # request identity -> last full key seen
        self.stats = {"hits": 0, "misses": 0, "puts": 0, "coalesced": 0,
                      "quarantined": 0, "evicted": 0,
                      "misses_new": 0, "misses_dependency_changed": 0}
        self._init_layout()

    # -- layout ----------------------------------------------------------------

    def _init_layout(self) -> None:
        for sub in ("objects", "quarantine", "tmp", "snapshots", "work"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        now = time.time()
        for stale in (self.root / "tmp").glob("put-*"):  # publish temps left by crashed writers
            with contextlib.suppress(OSError):
                if now - stale.stat().st_mtime > STALE_PUT_AGE:
                    stale.unlink()
        marker = self.root / "schema"
        if marker.exists() and marker.read_text(encoding="utf-8").strip() != str(CACHE_SCHEMA):
            # schema-versioned namespace: everything under an old schema is dead weight
            for sub in ("objects", "quarantine", "snapshots"):
                shutil.rmtree(self.root / sub, ignore_errors=True)
            for sub in ("objects", "quarantine", "snapshots"):
                (self.root / sub).mkdir(parents=True, exist_ok=True)
        marker.write_text(str(CACHE_SCHEMA), encoding="utf-8")

    def _entry_dir(self, key: str) -> Path:
        return self.root / "objects" / key

    # -- reads ------------------------------------------------------------------

    def _remember(self, request: str | None, key: str) -> None:
        if request is None:
            return
        self._requests.pop(request, None)
        self._requests[request] = key
        while len(self._requests) > REQUESTS_MAX:
            self._requests.pop(next(iter(self._requests)))

    def _miss(self, key: str, request: str | None) -> None:
        """Count a miss and its reason: "dependency-changed" when the same request identity
        was last seen under a different full key, else "new-input" (P2.8, P0.3)."""
        prev = self._requests.get(request) if request is not None else None
        reason = "dependency-changed" if prev is not None and prev != key else "new-input"
        self.stats["misses"] += 1
        self.stats["misses_dependency_changed" if reason == "dependency-changed"
                   else "misses_new"] += 1
        self._remember(request, key)
        telemetry.count(f"cache.miss.{reason}")

    def get(self, key: str, *, request: str | None = None) -> Entry | None:
        """`request` is the key's dependency-independent identity (`CompilationKey.request_id`);
        when given, a miss is classified as new input or changed dependencies."""
        d = self._entry_dir(key)
        with self._lock:
            if not d.is_dir():
                self._miss(key, request)
                return None
            try:
                meta = json.loads((d / "entry.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._quarantine(key, "unreadable entry manifest")
                self._miss(key, request)
                return None
            if meta.get("key") != key or meta.get("cache_schema") != CACHE_SCHEMA:
                self._quarantine(key, "key or schema mismatch")
                self._miss(key, request)
                return None
            if meta.get("manifest") is not None and sha(canonical(meta["manifest"])) != key:
                self._quarantine(key, "manifest does not match its key")
                self._miss(key, request)
                return None
            outcome = meta.get("outcome")
            if outcome == OUTCOME_SUCCESS:
                try:
                    payload = (d / "payload").read_bytes()
                except OSError:
                    self._quarantine(key, "missing payload")
                    self._miss(key, request)
                    return None
                if sha(payload) != meta.get("payload_sha256"):
                    self._quarantine(key, "payload hash mismatch")
                    self._miss(key, request)
                    return None
                entry = Entry(key, True, outcome, payload, meta.get("log", ""),
                              meta.get("created", 0.0))
            elif outcome == OUTCOME_ERROR:
                # an explicit cached negative -- never an empty object masquerading as ok
                entry = Entry(key, False, outcome, None, meta.get("log", ""),
                              meta.get("created", 0.0))
            else:
                self._miss(key, request)
                return None  # transient/unknown entries are never served
            try:
                os.utime(d / "entry.json", None)  # LRU touch
            except OSError:
                pass
            self.stats["hits"] += 1
            self._remember(request, key)
            telemetry.count("cache.hit")
            return entry

    # -- writes -----------------------------------------------------------------

    def put(self, key: str, *, ok: bool, outcome: str, payload: bytes | None, log: str,
            manifest: dict | None = None) -> None:
        if outcome not in (OUTCOME_SUCCESS, OUTCOME_ERROR):
            raise ValueError(f"transient outcome {outcome!r} must not be cached")
        if outcome == OUTCOME_SUCCESS and payload is None:
            raise ValueError("success entries need a payload")
        payload = payload or b""
        tmp = self.root / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        temps: list[str] = []  # temp files not yet renamed into place
        try:
            fd, pname = tempfile.mkstemp(dir=tmp, prefix="put-")
            temps.append(pname)
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            meta = {"key": key, "cache_schema": CACHE_SCHEMA, "ok": ok, "outcome": outcome,
                    "payload_sha256": sha(payload), "log": log, "created": time.time(),
                    "size": len(payload), "manifest": manifest}
            fd2, mname = tempfile.mkstemp(dir=tmp, prefix="put-")
            temps.append(mname)
            with os.fdopen(fd2, "w", encoding="utf-8") as f:
                json.dump(meta, f, sort_keys=True)
            size = len(payload) + len(log.encode("utf-8", "replace")) + 512
            with self._lock:
                d = self._entry_dir(key)
                d.mkdir(parents=True, exist_ok=True)
                os.replace(pname, d / "payload")   # payload first: the manifest is the commit point
                temps.remove(pname)
                os.replace(mname, d / "entry.json")
                temps.remove(mname)
                self._approx_bytes = (self._approx_bytes or 0) + size
                self.stats["puts"] += 1
        finally:
            for leftover in temps:  # a failed publish must not leave its temp files behind
                with contextlib.suppress(OSError):
                    os.unlink(leftover)
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        with self._lock:
            over = self._approx_bytes is not None and \
                self.max_bytes is not None and self._approx_bytes > self.max_bytes
        if over:
            self.evict()

    # -- quarantine ---------------------------------------------------------------

    def _quarantine(self, key: str, reason: str) -> None:
        try:
            q = self.root / "quarantine" / key
            n = 0
            while q.exists():
                n += 1
                q = Path(f"{q}.{n}")
            q.mkdir(parents=True)
            d = self._entry_dir(key)
            for name in ("payload", "entry.json"):
                src = d / name
                if src.exists():
                    try:
                        os.replace(src, q / name)
                    except OSError:
                        pass
            (q / "reason").write_text(reason, encoding="utf-8")
            try:
                d.rmdir()
            except OSError:
                pass
            self.stats["quarantined"] += 1
        except OSError:
            pass

    # -- quotas / eviction ----------------------------------------------------------

    def _delete_entry(self, key: str) -> bool:
        d = self._entry_dir(key)
        gone = False
        for name in ("payload", "entry.json"):
            try:
                (d / name).unlink()
                gone = True
            except OSError:
                pass
        try:
            d.rmdir()
        except OSError:
            pass
        return gone

    def evict(self, now: float | None = None) -> int:
        """Enforce size, age and entry-count quotas. Never evicts pinned (in-use) keys
        or pinned snapshots. Also sweeps stale quarantine/staging leftovers."""
        now = time.time() if now is None else now
        evicted = 0
        with self._lock:
            obj = self.root / "objects"
            # (kind, name, created, mtime, size); kind "o" = object entry, "s" = include snapshot
            rows: list[tuple[str, str, float, float, int]] = []
            approx = 0.0
            if obj.is_dir():
                for d in obj.iterdir():
                    if not d.is_dir():
                        continue
                    key = d.name
                    created = 0.0
                    size = 512
                    try:
                        meta = json.loads((d / "entry.json").read_text(encoding="utf-8"))
                        created = float(meta.get("created") or 0.0)
                        size += int(meta.get("size") or 0) + len(meta.get("log", ""))
                    except (OSError, ValueError, TypeError):
                        pass
                    try:
                        mtime = (d / "entry.json").stat().st_mtime
                    except OSError:
                        mtime = created
                    approx += size
                    rows.append(("o", key, created or mtime, mtime, size))
            # include snapshots count against the same size quota; in-use ones are pinned
            snaps = self.root / "snapshots"
            if snaps.is_dir():
                for d in snaps.iterdir():
                    try:
                        built = (d / "DONE").stat().st_mtime
                    except OSError:
                        continue
                    size = _tree_bytes(d)
                    approx += size
                    rows.append(("s", d.name, built, built, size))
            self._approx_bytes = approx
            rows.sort(key=lambda r: (r[3], r[1]))  # oldest first
            kept = 0
            for kind, name, created, _mtime, size in rows:
                if name in (self._pinned if kind == "o" else self._pinned_snaps):
                    kept += kind == "o"
                    continue
                too_old = self.max_age is not None and (now - created) > self.max_age
                too_many = kind == "o" and self.max_entries is not None \
                    and kept + 1 > self.max_entries
                too_big = self.max_bytes is not None and approx > self.max_bytes
                if too_old or too_many or too_big:
                    gone = self._delete_entry(name) if kind == "o" \
                        else _remove_tree(self.root / "snapshots" / name)
                    if gone:
                        approx -= size
                        evicted += 1
                    continue
                kept += kind == "o"
            self._approx_bytes = approx
            # staging and quarantine leftovers from crashed runs
            for sub in ("tmp", "quarantine"):
                base = self.root / sub
                if base.is_dir():
                    for d in base.iterdir():
                        try:
                            if now - d.stat().st_mtime > (self.max_age or DEFAULT_MAX_AGE):
                                shutil.rmtree(d, ignore_errors=True)
                        except OSError:
                            pass
            # stale deterministic staging dirs (kept at least a day: a compile in flight
            # owns its dir under the cross-process lease and never runs that long)
            work = self.root / "work"
            if work.is_dir():
                floor = max(86400.0, self.max_age or 0.0)
                for d in work.iterdir():
                    if not d.is_dir():
                        continue
                    try:
                        if now - d.stat().st_mtime > floor:
                            shutil.rmtree(d, ignore_errors=True)
                    except OSError:
                        pass
        self.stats["evicted"] += evicted
        return evicted

    # -- pinning (in-use payloads, refcounted) ---------------------------------------

    def pin(self, key: str) -> None:
        with self._lock:
            self._pinned[key] = self._pinned.get(key, 0) + 1

    def unpin(self, key: str) -> None:
        with self._lock:
            n = self._pinned.get(key, 0) - 1
            if n <= 0:
                self._pinned.pop(key, None)
            else:
                self._pinned[key] = n

    def pin_snapshot(self, dep_digest: str) -> None:
        with self._lock:
            self._pinned_snaps[dep_digest] = self._pinned_snaps.get(dep_digest, 0) + 1

    def unpin_snapshot(self, dep_digest: str) -> None:
        with self._lock:
            n = self._pinned_snaps.get(dep_digest, 0) - 1
            if n <= 0:
                self._pinned_snaps.pop(dep_digest, None)
            else:
                self._pinned_snaps[dep_digest] = n

    # -- transient short-retry policy -------------------------------------------------

    def record_transient(self, key: str, result: Any) -> None:
        with self._lock:
            self._transient[key] = (time.time(), result)

    def transient_get(self, key: str) -> Any | None:
        with self._lock:
            item = self._transient.get(key)
            if item is None:
                return None
            recorded, result = item
            if time.time() - recorded < self.transient_retry:
                return result
            del self._transient[key]
            return None

    # -- single-flight -------------------------------------------------------------

    def get_or_run(self, key: str, producer: Callable[[], Any], *,
                   wait_timeout: float | None = None,
                   cancel: Callable[[], Any] | None = None) -> Any:
        """Run `producer` once for `key`, coalescing concurrent callers onto the same
        flight. A waiter's timeout detaches only that waiter; when the last consumer
        detaches before completion, `cancel` gets a best-effort call so the producer can
        stop (P2.6.4)."""
        with self._lock:
            flight = self._flights.get(key)
            if flight is None:
                flight = _Flight(key, producer, cancel, threading.Condition(self._lock))
                flight.consumers = 1
                self._flights[key] = flight
                self.pin(key)  # an in-flight key's entry is in use
                thread = threading.Thread(target=self._flight_runner, args=(flight,),
                                          name=f"compile-cache-{key[:12]}", daemon=True)
                thread.start()
            else:
                flight.consumers += 1
                self.stats["coalesced"] += 1
        try:
            return flight.wait(wait_timeout)
        finally:
            self._detach(flight)

    def in_flight(self, key: str) -> bool:
        """True while a producer for `key` is still running. A waiter that timed out uses
        this to avoid starting a second compile for the same key."""
        with self._lock:
            flight = self._flights.get(key)
            return flight is not None and not flight.done

    def _detach(self, flight: _Flight) -> None:
        cancel_now = False
        with self._lock:
            flight.consumers -= 1
            if flight.consumers <= 0 and not flight.done and flight.cancel is not None:
                cancel_now = True
        if cancel_now:
            try:
                flight.cancel()
            except Exception:
                pass

    def _flight_runner(self, flight: _Flight) -> None:
        try:
            flight.run()
        finally:
            with self._lock:
                if self._flights.get(flight.key) is flight:
                    del self._flights[flight.key]
                self.unpin(flight.key)

    # -- snapshots ------------------------------------------------------------------

    def ensure_snapshot(self, key: CompilationKey) -> Path:
        """Build (once, content-verified) the read-only include snapshot for the key's
        dependency revision; return its include directory."""
        dep = key.dep_digest
        deps = key.dependencies
        final = self.root / "snapshots" / dep
        if (final / "DONE").exists():
            return final / "include"
        with self._lock:  # in-process serialisation; cross-process: atomic dir rename
            if (final / "DONE").exists():
                return final / "include"
            self.pin_snapshot(dep)
            tmp = self.root / "tmp" / f"snap-{dep}-{os.getpid()}-{threading.get_ident()}"
            try:
                if tmp.exists():
                    shutil.rmtree(tmp, ignore_errors=True)
                tmp_inc = tmp / "include"
                tmp_inc.mkdir(parents=True)
                for d in deps["dirs"]:
                    shutil.copytree(d["path"], tmp_inc, dirs_exist_ok=True, symlinks=True)
                (tmp_inc / "context.h").write_bytes(base64.b64decode(deps["header"]))
                _make_readonly(tmp_inc)
                # verify the copy matches the key's dependency identity before publishing
                facts = tree_facts(tmp_inc)
                if facts is None or sha(canonical(sorted(facts.files))) != deps["tree"]:
                    raise CacheBypass("include snapshot does not match its dependency digest")
                (tmp / "DONE").write_text(dep, encoding="utf-8")
                os.replace(tmp, final)  # atomic publication: DONE ⇔ complete snapshot
                try:
                    os.chmod(final, 0o555)  # the tree inside is read-only already
                except OSError:
                    pass
                # snapshots count against the size quota; the next put/evict enforces it
                self._approx_bytes = (self._approx_bytes or 0) + _tree_bytes(final)
            except CacheBypass:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            except OSError as e:
                shutil.rmtree(tmp, ignore_errors=True)
                if (final / "DONE").exists():  # another process published first: use theirs
                    return final / "include"
                raise CacheBypass(f"cannot build include snapshot: {e}") from e
            finally:
                self.unpin_snapshot(dep)
        return final / "include"

    # -- diagnostics -----------------------------------------------------------------

    def diagnose(self) -> str:
        with self._lock:
            entries = sum(1 for d in (self.root / "objects").iterdir() if d.is_dir()) \
                if (self.root / "objects").is_dir() else 0
            s = dict(self.stats)
            approx = self._approx_bytes
        total = s["hits"] + s["misses"]
        rate = f"{100 * s['hits'] / total:.1f}%" if total else "n/a"
        return (f"compile cache at {self.root}: {entries} entries, ~{approx or 0} bytes, "
                f"{s['hits']} hits / {s['misses']} misses ({rate}; "
                f"{s['misses_new']} new-input, "
                f"{s['misses_dependency_changed']} dependency-changed), {s['puts']} puts, "
                f"{s['coalesced']} coalesced waiters, {s['quarantined']} quarantined, "
                f"{s['evicted']} evicted; quotas: {self.max_bytes} bytes, "
                f"{self.max_age}s age, {self.max_entries or '-'} entries")


_CACHES: dict[Path, CompileCache] = {}
_CACHES_LOCK = threading.Lock()
_CACHES_MAX = 32


def cache_for_dir(root: Path | str) -> CompileCache | None:
    """The process-wide cache instance for `root` (None when it cannot be created)."""
    root = Path(root).resolve()
    with _CACHES_LOCK:
        c = _CACHES.get(root)
        if c is None:
            try:
                c = CompileCache(root)
            except OSError:
                return None
            _CACHES[root] = c
            while len(_CACHES) > _CACHES_MAX:
                _CACHES.pop(next(iter(_CACHES)))
        return c
