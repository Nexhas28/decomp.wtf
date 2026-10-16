"""Pinned compiler toolchains: registry, verified install, and lookup for the compile sandbox.

A toolchain is one or more upstream archives, each verified against a pinned sha256 and
extracted into its own directory. Projects name a toolchain in project.toml; the compiler
sandbox mounts that directory (and any runtime it requires) read-only.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from . import linuxbox
from .cache import canonical, sha

MANIFEST = ".decompnet-toolchain.json"
LINUX = "linux-x86_64"
# Parts preferred for the compiler container: Debian's build of a Linux program (a .deb, not Arch's .zst). The tag's
# old name stays: the registry is part of every profile's identity
DEB_PLATFORM = "wsl-x86_64"


class ToolchainError(RuntimeError):
    pass


@dataclass
class Toolchain:
    id: str
    name: str
    license: str
    source: str
    description: str = ""
    kind: str = "compiler"  # compiler | runtime | tool
    platform: str = ""
    arch: str = ""
    cc: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    cmd: list[str] = field(default_factory=list)  # full command template, when compiling is more than cc + flags
    parts: list[dict] = field(default_factory=list)
    git: str = ""
    commit: str = ""
    binary_name: str = ""  # main executable of a tool
    pip_extra: list[str] = field(default_factory=list)  # extra requirements for a git tool's environment
    requires_java: int = 0  # minimum Java version (Ghidra)

    @property
    def proprietary(self) -> bool:
        return self.license.lower() == "proprietary"

    def parts_for(self, plat: str | None = None) -> list[dict]:
        plat = plat or current_platform()
        return [p for p in self.parts if plat in p.get("platforms", [plat])]

    @property
    def via_linux(self) -> bool:
        """A Linux-only toolchain: installed from its Linux build and run in the compiler container (linuxbox.py)."""
        return (linuxbox.wanted() and not self.git and not self.parts_for()
                and bool(self.parts_for(LINUX)))

    def linux_parts(self) -> list[dict]:
        return [p for p in self.parts if DEB_PLATFORM in p.get("platforms", [])] or self.parts_for(LINUX)

    @property
    def supported(self) -> bool:
        """Whether this toolchain can be installed on this computer (Linux ones through the compiler container)."""
        return bool(self.git) or bool(self.parts_for()) or self.via_linux


def current_platform() -> str:
    """macos-x86_64 or macos-arm64 (registry parts also name linux-* and windows-* platforms)."""
    os_name = {"darwin": "macos"}.get(sys.platform, "linux" if sys.platform.startswith("linux") else sys.platform)
    machine = _platform.machine().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64", "arm64": "arm64" if os_name == "macos" else "aarch64",
               "aarch64": "aarch64" if os_name != "macos" else "arm64"}.get(machine, machine)
    return f"{os_name}-{machine}"


def unsupported_cpu() -> str | None:
    """Why this computer can't run the games' compilers, or None: they're x86 programs. A Mac runs them on any
    processor (natively, through Rosetta 2, or emulated in the compiler container)."""
    os_name, machine = current_platform().split("-", 1)
    if os_name == "macos" or machine == "x86_64":
        return None
    return (f"decomp.wtf needs a 64-bit Intel or AMD PC for now (the games' compilers are x86 programs; this "
            f"computer's processor is {machine})")


@functools.cache
def _registry_data() -> dict:
    return tomllib.loads(resources.files("decompnet").joinpath("toolchains.toml").read_text(encoding="utf-8"))


def registry() -> dict[str, Toolchain]:
    return {tid: Toolchain(id=tid, **spec) for tid, spec in _registry_data().items()}


def get(tid: str) -> Toolchain:
    reg = registry()
    if tid not in reg:
        raise ToolchainError(f"unknown toolchain {tid!r}; known: {', '.join(sorted(reg))}")
    return reg[tid]


def data_dir() -> Path:
    """Per-user data directory for decompnet (toolchains, caches)."""
    env = os.environ.get("XDG_DATA_HOME")
    if env:  # set explicitly: it wins
        return Path(env) / "decompnet"
    return Path.home() / "Library" / "Application Support" / "decompnet"


def root() -> Path:
    env = os.environ.get("DECOMPNET_TOOLCHAINS")
    return Path(env).expanduser() if env else data_dir() / "toolchains"


def path(tid: str, linux: bool | None = None) -> Path:
    """Where a toolchain is installed. `linux`: its Linux build for the compiler container (default: when that's how
    it runs here)."""
    if linux is None:
        linux = tid in _registry_data() and get(tid).via_linux
    return root() / "linux" / tid if linux else root() / tid


def installed(tid: str, linux: bool | None = None) -> dict | None:
    m = path(tid, linux) / MANIFEST
    return json.loads(m.read_text(encoding="utf-8")) if m.exists() else None


def require(tid: str, linux: bool | None = None) -> Path:
    """Directory of an installed toolchain, or a clear error telling the operator how to install it."""
    if installed(tid, linux) is None:
        raise ToolchainError(f"toolchain {tid!r} is not installed: run `decompnet toolchain install {tid}`")
    return path(tid, linux)


def runtime_dirs(tid: str) -> tuple[Path, ...]:
    """The toolchain plus everything it requires (all Linux builds for the compiler container)."""
    tc = get(tid)
    linux = tc.via_linux
    return (require(tid, linux), *(require(dep, linux) for dep in tc.requires))


# ---------------------------------------------------------------------------- install

def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path, log) -> None:
    log(f"  downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "decompnet-toolchain-installer"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def _deb_data(deb: Path, out: Path) -> Path:
    """The data.tar.* inside a Debian package (an `ar` archive), written to `out`."""
    with open(deb, "rb") as f:
        if f.read(8) != b"!<arch>\n":
            raise ToolchainError(f"{deb.name} isn't a Debian package")
        while len(head := f.read(60)) == 60:
            name, size = head[:16].decode().strip().rstrip("/"), int(head[48:58].decode().strip())
            data = f.read(size)
            if size % 2:
                f.read(1)
            if name.startswith("data.tar"):
                (out / name).write_bytes(data)
                return out / name
    raise ToolchainError(f"{deb.name} has no data.tar")


def _extract(archive: Path, dest: Path, subdir: str | None) -> None:
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        if archive.name.endswith(".deb"):
            with tempfile.TemporaryDirectory(dir=dest.parent) as raw:
                with tarfile.open(_deb_data(archive, Path(raw))) as t:
                    t.extractall(tmp, filter="data")
        elif archive.name.endswith(".zip"):
            import zipfile

            with zipfile.ZipFile(archive) as z:
                for info in z.infolist():
                    target = (Path(tmp) / info.filename).resolve()
                    if Path(tmp).resolve() not in target.parents and target != Path(tmp).resolve():
                        raise ToolchainError(f"refusing to extract {info.filename} outside the toolchain folder")
                    z.extract(info, tmp)
                    mode = info.external_attr >> 16
                    if mode & 0o111 and not info.is_dir():
                        os.chmod(target, 0o755)
        elif archive.name.endswith(".zst"):
            # GNU tar extracts it; never restore owners from the archive. The zstd layer is undone here first: tar
            # would run the zstd command for it, which many systems don't have (tar then just fails with code 2)
            import zstandard

            plain = Path(tmp).with_name(Path(tmp).name + ".tar")
            try:
                with open(archive, "rb") as src, open(plain, "wb") as out:
                    zstandard.ZstdDecompressor().copy_stream(src, out)
                subprocess.run(["tar", "-xf", str(plain), "-C", tmp, "--no-same-owner", "--no-same-permissions"],
                               check=True, capture_output=True)
            finally:
                plain.unlink(missing_ok=True)
        else:
            # gz/xz in pure Python, so no tar program is needed. The "data" filter refuses
            # absolute paths, links out of the tree and device files, and drops setuid bits.
            with tarfile.open(archive) as t:
                t.extractall(tmp, filter="data")
        src = Path(tmp) / subdir if subdir else Path(tmp)
        if not src.is_dir():
            raise ToolchainError(f"archive {archive.name} has no {subdir}/")
        for item in src.iterdir():
            target = dest / item.name
            if target.exists():
                shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink()
            shutil.move(str(item), target)


def install(tid: str, *, accept_license: bool = False, force: bool = False, log=print, linux: bool | None = None) -> Path:
    """Install a toolchain (and what it requires). `linux`: its Linux build, to run in the compiler container
    (default: when that's how it runs on this computer)."""
    tc = get(tid)
    linux = tc.via_linux if linux is None else linux
    if linux:
        ok, why = linuxbox.status()
        if not ok:
            raise ToolchainError(f"{tc.name} is a Linux program: on a Mac it runs in a Docker container. {why}")
    for dep in tc.requires:
        if installed(dep, linux) is None or force:
            install(dep, accept_license=accept_license, force=force, log=log, linux=linux)
    if installed(tid, linux) and not force:
        log(f"{tid}: already installed at {path(tid, linux)}")
        return path(tid, linux)
    parts = tc.linux_parts() if linux else tc.parts_for()
    if not parts and not tc.git:
        why = unsupported_cpu()
        raise ToolchainError(f"{tid} ({tc.name}) has no build for this computer ({current_platform()})"
                             + (f": {why}" if why else ""))
    if tc.requires_java and java_version() < tc.requires_java:
        raise ToolchainError(f"{tc.name} needs Java {tc.requires_java} or newer: install it from https://adoptium.net first")
    if tc.proprietary and not accept_license:
        raise ToolchainError(
            f"{tid} ({tc.name}) is proprietary software. Running it on your coordinator is your own licensing "
            f"decision; re-run with --accept-license if you accept that.")

    dest = path(tid, linux)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    log(f"{tid}: installing {tc.name} [{tc.license}]" + (" (Linux build, runs in the compiler container (Docker))"
                                                          if linux else ""))
    pinned = []
    try:
        if tc.git:
            _install_git_tool(tc, dest, log)
        downloads = root() / ".downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        for part in parts:
            archive = downloads / Path(part["url"]).name
            if not archive.exists() or (part.get("sha256") and _sha256(archive) != part["sha256"]):
                _download(part["url"], archive, log)
            digest = _sha256(archive)
            if part.get("sha256") and digest != part["sha256"]:
                archive.unlink()
                raise ToolchainError(f"checksum mismatch for {part['url']}: expected {part['sha256']}, got {digest}")
            if not part.get("sha256"):
                log(f"  no pinned checksum in the registry; pinning on first use: sha256 {digest}")
            if part.get("binary"):  # a plain executable, not an archive
                shutil.copyfile(archive, dest / part["binary"])
                os.chmod(dest / part["binary"], 0o755)
            else:
                _extract(archive, dest, part.get("subdir"))
                if part.get("executable"):  # archives that don't record the executable bit
                    for f in dest.rglob("*"):
                        if f.is_file():
                            os.chmod(f, 0o755)
            pinned.append({"url": part["url"], "sha256": digest})
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    (dest / MANIFEST).write_text(json.dumps({
        "id": tid, "name": tc.name, "license": tc.license, "source": tc.source,
        "parts": pinned, "commit": tc.commit or None, "installed_at": time.time(),
        "license_accepted": bool(tc.proprietary and accept_license), "linux": linux,
    }, indent=2), encoding="utf-8")
    log(f"{tid}: installed at {dest}")
    return dest


def venv_bin(venv: Path, name: str) -> Path:
    """Path of an executable inside a virtual environment."""
    return venv / "bin" / name


def _install_git_tool(tc: Toolchain, dest: Path, log) -> None:
    uv = shutil.which("uv")
    if not uv:
        raise ToolchainError(f"{tc.id} needs `uv` to create its isolated environment")
    if not shutil.which("git"):
        # uv can fetch the pinned commit as an archive when git is missing
        spec = f"{tc.id} @ {tc.git.removesuffix('.git')}/archive/{tc.commit}.zip"
    else:
        spec = f"{tc.id} @ git+{tc.git}@{tc.commit}"
    log(f"  creating an isolated environment for {tc.git} @ {tc.commit[:10]}")
    subprocess.run([uv, "venv", "--quiet", "--python", "3.12", str(dest / "venv")], check=True)
    subprocess.run([uv, "pip", "install", "--quiet", "--python", str(venv_bin(dest / "venv", "python")), spec, *tc.pip_extra],
                   check=True)


def binary(tid: str) -> Path:
    """The main executable of an installed tool (objdiff-cli, dtk, coddog, ...)."""
    tc = get(tid)
    base = require(tid)
    name = tc.binary_name or tid
    for candidate in (base / name, base / "bin" / name):
        if candidate.exists():
            return candidate
    raise ToolchainError(f"{tid} is installed but {name} wasn't found in {base}")


def ensure(tid: str, log=print) -> Path:
    """Install a free tool on first use and return its folder. Proprietary ones must be installed explicitly."""
    if installed(tid) is None:
        tc = get(tid)
        if tc.proprietary:
            raise ToolchainError(f"{tc.name} is proprietary: install it from Setup if you accept its license")
        install(tid, log=log)
    return path(tid)


def java_version() -> int:
    """Major version of the java on PATH, 0 if none."""
    import re as _re

    exe = shutil.which("java")
    if not exe:
        return 0
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=20,
                             encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return 0
    m = _re.search(r'version "(\d+)', out.stderr + out.stdout)
    return int(m.group(1)) if m else 0


def side_status(prepare: bool = False, log=print) -> tuple[bool, str]:
    """Whether the compiler container for Linux-only toolchains works here: (usable, why not). `prepare` builds
    the compiler image if it's missing."""
    if prepare:
        try:
            linuxbox.prepare(log)
        except RuntimeError as e:
            return False, f"on a Mac it runs in a Docker container. {e}"
    ok, why = linuxbox.status()
    return ok, "" if ok else f"on a Mac it runs in a Docker container. {why}"


def status() -> list[dict]:
    rows = []
    for tc in registry().values():
        m = installed(tc.id)
        rows.append({"id": tc.id, "name": tc.name, "kind": tc.kind, "platform": tc.platform, "license": tc.license,
                     "installed": m is not None, "supported": tc.supported, "description": tc.description,
                     "linux": tc.via_linux,
                     "pinned": all(p.get("sha256") for p in tc.parts) or bool(tc.commit)})
    return rows


# ---------------------------------------------------------------------------- profiles
# Explicit, scoped compiler profiles and bounded calibration (plan P4.1).
#
# A Profile is the complete compilation identity a project works under: toolchain, wrapper,
# ordered flags, language/dialect, processor mode, ordered ABI settings and source-unit scope.
# Its identity digest covers every one of those fields PLUS the pinned registry specification
# and the recorded installation manifests (.decompnet-toolchain.json) of the toolchain and
# everything it requires -- so a flag change, a wrapper change, or a toolchain reinstalled
# with different pinned binaries all produce a different identity.
#
# Invalidation contract (P4.1.8): a profile-identity change is the signal callers use to
# invalidate compilation cache entries, m2c/context drafts, prepared scores and search
# budgets keyed under the old identity. `profile_diff` is the changelog helper that reports
# which fields changed so the engine can log the change; provenance is recorded with every
# profile but deliberately does NOT participate in the identity (a guess confirmed later
# compiles identically, so re-labelling it must not flush caches). Imported guesses remain
# hypotheses until something explicitly re-labels them: calibrate() below returns a
# recommendation record and never mutates project configuration itself -- the caller/engine
# decides what to commit (P4.1.2, P4.1.7).

PROFILE_IDENTITY_SCHEMA = 1  # bump when the identity manifest's shape changes (backstop)
PROVENANCE_GUESS = "guess"
PROVENANCE_EXPLICIT = "explicit"
PROVENANCE_CALIBRATED = "calibrated"
PROVENANCE = (PROVENANCE_GUESS, PROVENANCE_EXPLICIT, PROVENANCE_CALIBRATED)
# Cache/budget classes keyed by profile identity; an identity change invalidates them all.
INVALIDATED = ("compilation", "drafts", "scores", "search_budgets")
_IDENTITY_FIELDS = ("toolchain", "wrapper", "flags", "language", "processor_mode", "abi", "scope", "timeout")


@dataclass(frozen=True)
class Profile:
    """An explicit, scoped compiler profile (plan P4.1).

    Fields:
        toolchain:      registry id ("" = no compiler configured).
        flags:          ordered extra flags (the caller resolves toolchain defaults).
        wrapper:        which registry invocation to use: "cc" (the toolchain's compiler
                        template) or "cmd" (its full command template, e.g. agbcc).
        language:       source language / dialect, "c" or "c++".
        processor_mode: e.g. "mips3" or "thumb"; "" = the toolchain's default.
        abi:            ordered ABI-relevant settings as (key, value) pairs.
        scope:          source-unit scope this profile governs; "" = every unit, a non-empty
                        value names one unit (or a unit pattern) so projects can hold more
                        than one profile where translation units legitimately differ.
        provenance:     how the profile was chosen: "guess" (imported hypothesis),
                        "explicit" (operator-set) or "calibrated". Metadata only: excluded
                        from the identity digest on purpose.
        timeout:        compile execution budget; part of the identity when set (keeps
                        drop-in compatibility with the legacy compiler digest, which
                        hashed it).
    """

    toolchain: str
    flags: tuple[str, ...] = ()
    wrapper: str = "cc"
    language: str = "c"
    processor_mode: str = ""
    abi: tuple[tuple[str, str], ...] = ()
    scope: str = ""
    provenance: str = PROVENANCE_GUESS
    timeout: float | None = None

    def __post_init__(self):
        if self.provenance not in PROVENANCE:
            raise ValueError(f"provenance must be one of {PROVENANCE} (got {self.provenance!r})")
        # normalise list inputs so Profile(flags=[...], abi=[...]) equals the tuple form
        object.__setattr__(self, "flags", tuple(str(f) for f in self.flags))
        object.__setattr__(self, "abi", _abi_pairs(self.abi))

    def identity(self, *, include_installed: bool = True) -> str:
        """The profile's version identity.

        Covers the whole profile (toolchain, wrapper, ordered flags, language, processor
        mode, ordered ABI settings, scope, timeout when set) AND the pinned registry
        specification and recorded installation manifests of the toolchain and everything
        it requires. A flag change, a wrapper change, or a toolchain reinstalled with
        different pinned binaries therefore changes the identity.

        Invalidation contract (P4.1.8): callers treat an identity change as the signal to
        invalidate compilation/draft/score caches and search budgets keyed under the old
        identity; provenance is excluded (see the module section comment). With
        `include_installed=False` the digest is host-independent (registry pins only), for
        cross-host comparisons such as remote-worker snapshot fields.
        """
        closure = _toolchain_closure(self.toolchain)
        registry_part, installed_part = {}, {}
        for tid in closure:
            registry_part[tid] = _registry_identity(tid)
            installed_part[tid] = _installed_manifest_identity(tid) if include_installed else None
        manifest_part = {
            "toolchain": self.toolchain, "wrapper": self.wrapper, "flags": list(self.flags),
            "language": self.language, "processor_mode": self.processor_mode,
            "abi": [list(pair) for pair in self.abi], "scope": self.scope,
        }
        if self.timeout is not None:
            manifest_part["timeout"] = self.timeout
        return sha(canonical({"profile_identity_schema": PROFILE_IDENTITY_SCHEMA,
                              "profile": manifest_part, "toolchains": registry_part,
                              "installed": installed_part}))

    def to_dict(self) -> dict:
        d = {"toolchain": self.toolchain, "wrapper": self.wrapper, "flags": list(self.flags),
             "language": self.language, "processor_mode": self.processor_mode,
             "abi": [list(pair) for pair in self.abi], "scope": self.scope,
             "provenance": self.provenance}
        if self.timeout is not None:
            d["timeout"] = self.timeout
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        return profile_from(d)

    def describe(self) -> str:
        flags = f" [{' '.join(self.flags)}]" if self.flags else ""
        return f"{self.toolchain or '-'}{flags} ({self.language}; {self.provenance})"


def _abi_pairs(raw) -> tuple[tuple[str, str], ...]:
    """ABI settings as ordered (key, value) pairs, from a dict or a list of pairs."""
    if not raw:
        return ()
    pairs = raw.items() if isinstance(raw, dict) else raw
    out = []
    for item in pairs:
        if isinstance(item, (list, tuple)):
            out.append((str(item[0]), str(item[1]) if len(item) > 1 else ""))
        else:
            out.append((str(item), ""))
    return tuple(out)


def profile_from(compiler: dict) -> Profile:
    """A Profile from a config's compiler dict (legacy `{"toolchain", "flags", "timeout"}`
    and any extended keys). Unknown provenance labels are coerced to "guess" so configs
    written by older or newer versions never break identity computation."""
    if not isinstance(compiler, dict):
        raise TypeError(f"compiler config must be a dict, not {type(compiler).__name__}")
    provenance = compiler.get("provenance") or PROVENANCE_GUESS
    if provenance not in PROVENANCE:
        provenance = PROVENANCE_GUESS
    timeout = compiler.get("timeout")
    try:
        timeout = float(timeout) if timeout is not None else None
    except (TypeError, ValueError):
        timeout = None
    return Profile(toolchain=str(compiler.get("toolchain") or ""),
                   flags=tuple(str(f) for f in (compiler.get("flags") or [])),
                   wrapper=str(compiler.get("wrapper") or "cc"),
                   language=str(compiler.get("language") or "c"),
                   processor_mode=str(compiler.get("processor_mode") or ""),
                   abi=_abi_pairs(compiler.get("abi")),
                   scope=str(compiler.get("scope") or ""),
                   provenance=provenance, timeout=timeout)


def profile_of(proj_or_config) -> Profile:
    """The profile behind a Profile, a compiler dict, a parsed project config (with or
    without a "compiler" key), or a project row (its "config" JSON). Configs without any
    compiler section yield the empty profile `Profile("")`, like the legacy digest."""
    if isinstance(proj_or_config, Profile):
        return proj_or_config
    if not isinstance(proj_or_config, dict):  # a project row: config is stored as JSON
        try:
            cfg = json.loads(proj_or_config["config"])
        except (KeyError, TypeError, ValueError):
            return Profile("")
    else:
        cfg = proj_or_config
        if "config" in cfg and isinstance(cfg["config"], dict):  # an already-parsed row
            cfg = cfg["config"]
        elif "config" in cfg and isinstance(cfg["config"], str):
            try:
                cfg = json.loads(cfg["config"])
            except ValueError:
                return Profile("")
    if not isinstance(cfg, dict):
        return Profile("")
    compiler = cfg.get("compiler", cfg)
    return profile_from(compiler) if isinstance(compiler, dict) else Profile("")


def profile_identity(proj_or_config, *, include_installed: bool = True) -> str:
    """The profile identity behind `proj_or_config` (any shape `profile_of` accepts).

    This is the versioning/invalidation signal of P4.1.8: when it changes, compilation
    cache entries, drafts, scores and search budgets keyed under the old identity are
    stale. `snapshots.compiler_digest` should be backed by this function so the acceptance
    gate (V-05) sees wrapper and binary-manifest changes, not only config-dict changes.
    """
    return profile_of(proj_or_config).identity(include_installed=include_installed)


def _toolchain_closure(tid: str) -> list[str]:
    """The profile's toolchain plus everything its invocation requires (registry `requires`)."""
    seen, order = set(), []
    todo = [tid] if tid else []
    while todo:
        current = todo.pop(0)
        if not current or current in seen:
            continue
        seen.add(current)
        order.append(current)
        spec = _registry_data().get(current)
        if spec:
            todo.extend(spec.get("requires") or [])
    return order


def _registry_identity(tid: str) -> dict | None:
    """The pinned, compile-relevant part of a registry entry (None: unknown toolchain)."""
    spec = _registry_data().get(tid)
    if spec is None:
        return None
    return {"cc": spec.get("cc") or [], "cmd": spec.get("cmd") or [], "flags": spec.get("flags") or [],
            "arch": spec.get("arch") or "", "platform": spec.get("platform") or "",
            "requires": spec.get("requires") or [],
            "parts": [{"url": p.get("url"), "sha256": p.get("sha256")} for p in spec.get("parts") or []],
            "git": spec.get("git") or "", "commit": spec.get("commit") or ""}


def _installed_manifest_identity(tid: str) -> dict | None:
    """The pinned part of a recorded installation manifest (None: not installed / unreadable).
    `installed_at` and `license_accepted` are excluded: an identical reinstall keeps the
    same identity, a reinstall with different pinned binaries changes it."""
    try:
        m = installed(tid)
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict):
        return None
    # "wsl" is always False now; the key stays so existing profile identities don't change
    return {"parts": m.get("parts") or [], "commit": m.get("commit"), "wsl": bool(m.get("wsl"))}


@dataclass(frozen=True)
class ProfileChange:
    """One versioned profile change: the changelog record callers persist with events."""
    before_identity: str
    after_identity: str
    changed: tuple[str, ...] = ()
    provenance: tuple[str, str] = (PROVENANCE_GUESS, PROVENANCE_GUESS)
    invalidates: tuple[str, ...] = ()
    note: str = ""

    @property
    def is_identity_change(self) -> bool:
        return self.before_identity != self.after_identity

    def as_dict(self) -> dict:
        return {"before_identity": self.before_identity, "after_identity": self.after_identity,
                "changed": list(self.changed), "provenance": list(self.provenance),
                "invalidates": list(self.invalidates), "note": self.note}


def profile_diff(before, after, note: str = "") -> ProfileChange:
    """The changelog helper: which identity fields changed between two profiles (Profiles
    or compiler dicts), and what an identity change invalidates (P4.1.8). Provenance
    transitions are reported alongside but never change the identity."""
    b = before if isinstance(before, Profile) else profile_from(before)
    a = after if isinstance(after, Profile) else profile_from(after)
    changed = tuple(f for f in _IDENTITY_FIELDS if getattr(b, f) != getattr(a, f))
    identity_changed = b.identity() != a.identity()
    return ProfileChange(before_identity=b.identity(), after_identity=a.identity(),
                         changed=changed, provenance=(b.provenance, a.provenance),
                         invalidates=INVALIDATED if identity_changed else (), note=note)


# ---------------------------------------------------------------------------- calibration

@dataclass(frozen=True)
class SampleResult:
    """The judge's answer for one sample item under one profile."""
    index: int
    verdict: str  # "match" | "wrong" | "inconclusive"
    score: float | None
    evidence: str = ""


@dataclass(frozen=True)
class ProfileResult:
    """Per-profile calibration outcome. `truncated`: the sample was cut short by an explicit
    wrong verdict or a judge error, so the outcome covers a partial sample."""
    profile: Profile
    identity: str
    verdict: str
    score: float | None
    samples: tuple[SampleResult, ...] = ()
    evidence: tuple[str, ...] = ()
    truncated: bool = False
    error: str = ""


@dataclass(frozen=True)
class CalibrationReport:
    """Bounded calibration outcome. Never carries or mutates project configuration: the
    recommendation is a record the caller/engine decides whether to commit."""
    status: str  # "recommended" | "inconclusive" | "ambiguous"
    recommendation: Profile | None
    recommendation_identity: str | None
    results: tuple[ProfileResult, ...] = ()
    skipped: tuple[Profile, ...] = ()
    judge_calls: int = 0
    max_profiles: int = 0
    max_samples: int = 0
    note: str = ""


def _judge_outcome(result) -> tuple[str, float | None, str]:
    """Normalise a judge's per-profile answer to (verdict, score, evidence). Verdicts come
    only from the judge: a bare score, or a dict without a verdict, is "inconclusive" --
    calibration never infers a verdict from flags, drafts or sample content."""
    if isinstance(result, bool):
        return ("match" if result else "wrong", None, "")
    if isinstance(result, (int, float)):
        return ("inconclusive", float(result), "")
    if isinstance(result, dict):
        verdict = result.get("verdict")
        if verdict is None:
            match = result.get("match")
            verdict = "match" if match is True else "wrong" if match is False else None
        if verdict not in ("match", "wrong", "inconclusive"):
            verdict = "inconclusive"
        score = result.get("score")
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        return (verdict, score, str(result.get("evidence") or ""))
    return ("inconclusive", None, "")


def calibrate(candidates, sample, judge, *, max_profiles: int = 6, max_samples: int = 3,
              note: str = "") -> CalibrationReport:
    """Bounded compiler-profile calibration (plan P4.1.4).

    Tries a small, caller-bounded hypothesis set against a small representative function
    sample and reports per-profile results, inconclusive outcomes and a recommendation
    record. The operation is pure: it never touches project configuration -- the caller
    (the engine) decides what to commit, after held-out and accepted-match validation
    (P4.1.7).

    Budget: at most `max_profiles` candidates are evaluated with at most `max_samples`
    sample items each (`max_profiles * max_samples` judge calls); candidates beyond the
    budget are reported as skipped. A profile the judge explicitly rejects stops consuming
    its sample; judge exceptions are recorded as inconclusive, never fatal.

    Source uncertainty stays separate from compiler uncertainty (P4.1.5): a poor draft
    alone never marks a profile wrong -- only the judge's per-profile verdict does. A
    profile is recommended only when it is the unique candidate the judge confirmed across
    the whole evaluated sample; several confirmed candidates stay ambiguous and nothing is
    recommended. The judge receives `(profile, asm, data, context_header)` and returns a
    bool, a score, or `{"verdict"|"match", "score", "evidence"}`.
    """
    if not callable(judge):
        raise TypeError("judge must be callable")
    profiles = [c if isinstance(c, Profile) else profile_from(c) for c in candidates]
    evaluated, skipped = profiles[:max_profiles], tuple(profiles[max_profiles:])
    sample_items = list(sample)[:max_samples]
    results, calls = [], 0
    for cand in evaluated:
        outcomes, error = [], ""
        for index, item in enumerate(sample_items):
            asm, data, header = item
            calls += 1
            try:
                verdict, score, evidence = _judge_outcome(judge(cand, asm, data, header))
            except Exception as e:  # the judge is an external hypothesis test: bound it
                error = str(e) or type(e).__name__
                outcomes.append(SampleResult(index, "inconclusive", None, f"judge error: {error}"))
                break
            outcomes.append(SampleResult(index, verdict, score, evidence))
            if verdict == "wrong":
                break  # an explicit per-profile rejection: don't spend the rest of the sample
        verdicts = [o.verdict for o in outcomes]
        if any(v == "wrong" for v in verdicts):
            verdict = "wrong"
        elif any(v == "inconclusive" for v in verdicts):
            verdict = "inconclusive"
        elif verdicts:
            verdict = "match"
        else:
            verdict = "inconclusive"  # nothing evaluated (empty sample or empty budget)
        scores = [o.score for o in outcomes if o.score is not None]
        results.append(ProfileResult(cand, cand.identity(), verdict,
                                     sum(scores) / len(scores) if scores else None,
                                     tuple(outcomes), tuple(o.evidence for o in outcomes),
                                     len(outcomes) < len(sample_items), error))
    matches = [r for r in results if r.verdict == "match"]
    if len(matches) == 1:
        status, recommendation = "recommended", matches[0].profile
    elif len(matches) > 1:
        status, recommendation = "ambiguous", None
    else:
        status, recommendation = "inconclusive", None
    return CalibrationReport(status, recommendation,
                             recommendation.identity() if recommendation else None,
                             tuple(results), skipped, calls, max_profiles, max_samples, note)


# ----------------------------------------------- opt-in post-assembler asm passes (plan C.2)

# Nothing here is active by default: a project opts in by naming a pass in its compiler profile
# ({"compiler": {..., "asm_pass": "maspsx-drop-delay-nops"}}); compiler.py consumes it in the
# production wiring. The maspsx pass is the C.2 spike: drop load-delay nops before j/jr where
# maspsx semantics allow it (see decompnet.rom.maspsx).
_ASM_PASSES: dict[str, object] = {}


def register_asm_pass(name: str, fn) -> None:
    """Register a named post-assembler asm-text pass. Names are version-gated like the rest of the
    registry: the baseline name cannot be redefined."""
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
        raise ValueError(f"asm pass name must match [a-z0-9][a-z0-9-]*: {name!r}")
    if name in _ASM_PASSES:
        raise ValueError(f"asm pass {name!r} is already registered")
    _ASM_PASSES[name] = fn


def asm_pass(name: str):
    """The named post-assembler pass callable, or None when this process doesn't know the name."""
    return _ASM_PASSES.get(name)


def apply_asm_pass(name: str, asm_text: str) -> str:
    """Apply a registered post-assembler pass to asm text and return the new text. Passes return
    ``(text, report)``; the report is discarded here (search-record wiring consumes it separately).
    Unknown names are rejected explicitly."""
    fn = _ASM_PASSES.get(name)
    if fn is None:
        raise ValueError(f"unknown asm pass {name!r} (registered: {', '.join(sorted(_ASM_PASSES)) or 'none'})")
    result = fn(asm_text)
    return result[0] if isinstance(result, tuple) else result


def _register_builtin_asm_passes() -> None:
    from .rom.maspsx import drop_delay_nops, expand_move_addu
    register_asm_pass("maspsx-drop-delay-nops", drop_delay_nops)
    register_asm_pass("psyq-move-addu", expand_move_addu)


_register_builtin_asm_passes()


@dataclass(frozen=True)
class CalibrationCommit:
    """The outcome of a calibrate-and-commit run (plan P4.1.7). Never contains project
    state: the commit itself is the caller's hook, invoked only after validation passed."""
    committed: bool
    reason: str
    profile: Profile | None
    identity: str | None
    report: CalibrationReport
    held_out: tuple[SampleResult, ...] = ()
    held_out_cut: int = 0
    regression: tuple[str, str] = ("not-run", "no regression hook supplied")


def calibrate_and_commit(candidates, sample, judge, *, held_out=(), held_out_judge=None,
                         regression=None, commit=None, max_profiles: int = 6, max_samples: int = 3,
                         max_held_out: int = 4, note: str = "") -> CalibrationCommit:
    """Bounded calibration that only commits a recommendation after held-out validation
    (plan P4.1.4/P4.1.7), and never mutates anything itself: `commit` is the caller's hook
    and is invoked exactly once, with the validated profile, only when every gate passed.

    Gates, in order -- any refusal stops before the next gate and the record says why:
      1. the calibration must recommend a single profile (inconclusive and ambiguous
         results never commit);
      2. a held-out set must be supplied (the default is empty = refuse: explicit opt-in),
         and every held-out sample item must validate ("match") under `held_out_judge`
         (defaults to `judge`; an independent validator is recommended);
      3. the `regression` hook -- the caller's re-check of affected accepted matches --
         must be supplied and answer (True, ...); a False answer refuses.
    The held-out set is bounded: at most `max_held_out` items are validated and the rest
    reported as `held_out_cut`. The commit hook's exceptions propagate to the caller (a
    half-applied commit must not be recorded as committed).
    """
    report = calibrate(candidates, sample, judge, max_profiles=max_profiles,
                       max_samples=max_samples, note=note)
    if report.status != "recommended":
        return CalibrationCommit(False, f"calibration is {report.status}: no recommendation to commit",
                                 None, None, report)
    if not held_out:
        return CalibrationCommit(False, "no held-out validation set supplied: refusing to commit "
                                        "(an explicit held-out set is required)", None, None, report)
    if commit is None:
        return CalibrationCommit(False, "no commit hook supplied: refusing to commit", None, None, report)

    profile = report.recommendation
    validator = held_out_judge or judge
    outcomes, cut = [], max(0, len(held_out) - max_held_out)
    for index, item in enumerate(held_out[:max_held_out]):
        asm, data, header = item
        try:
            verdict, score, evidence = _judge_outcome(validator(profile, asm, data, header))
        except Exception as e:  # a failing validator is not a validation
            outcomes.append(SampleResult(index, "inconclusive", None, f"judge error: {e}"))
            continue
        outcomes.append(SampleResult(index, verdict, score, evidence))
    if any(o.verdict != "match" for o in outcomes):
        bad = ", ".join(f"{o.index}: {o.verdict}" for o in outcomes if o.verdict != "match")
        return CalibrationCommit(False, f"held-out validation disagreed ({bad}): refusing to commit",
                                 None, None, report, tuple(outcomes), cut)

    regression_outcome = ("not-run", "no regression hook supplied")
    if regression is None:
        return CalibrationCommit(False, "no accepted-match regression hook supplied: refusing to commit",
                                 None, None, report, tuple(outcomes), cut, regression_outcome)
    try:
        ok, why = regression(profile)
        regression_outcome = ("ok" if ok else "failed", str(why))
    except Exception as e:  # the re-check itself failed: treat as failed, never commit
        regression_outcome = ("failed", f"regression check raised: {e}")
    if regression_outcome[0] != "ok":
        return CalibrationCommit(False, f"accepted-match regression failed: {regression_outcome[1]}",
                                 None, None, report, tuple(outcomes), cut, regression_outcome)

    commit(profile)
    return CalibrationCommit(True, "committed after held-out validation", profile, profile.identity(),
                             report, tuple(outcomes), cut, regression_outcome)


