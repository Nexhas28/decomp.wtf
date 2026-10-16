"""Ghidra evidence: structured function dossiers for platforms m2c doesn't read (x86, AArch64, Xbox 360
PowerPC, SuperH...).

Optional: Ghidra is large (540 MB) and needs Java 21+. Headless Ghidra analyses a unit's target object once
and exports a structured dossier for every function; the result is cached next to the project, so the cost
(tens of seconds, mostly starting the JVM) is paid once per source file.

Phase 7 replaces the reviewed name-to-C mapping with a versioned evidence contract:

- target admission (P7.1): a :class:`TargetProfile` pins the target kind (relocatable object, linked
  executable or raw region), processor/endianness/mode, memory map, authoritative symbols and function
  boundaries. Profiles that contradict the artifact bytes, overlap in memory, or name an unsupported
  language are rejected with explicit reasons (:class:`AdmissionError`) instead of being guessed. File
  offsets, original virtual addresses, analysis addresses and object-section offsets are kept as separate
  coordinates (:class:`Coordinates`).
- a function dossier (P7.3): entry/namespace/body ranges/thunk classification per function, pseudocode with
  status and warnings, instruction, basic-block, call and data/string/literal observations, recovered types
  with origin, optional bounded p-code, and a per-facet coverage map in which unknown, unavailable, empty,
  failed, partial and ok are different values (:class:`FunctionDossier`).
- explicit completion metadata (P7.4): the headless exit status, timeout and cancellation are checked; a
  complete manifest is published atomically; partial results are cached only when their coverage is known;
  transient failures (timeout, crash, nonzero exit without rows) are never cached as complete-empty
  success. The cache key covers the target bytes, loader/language/mode, Ghidra version, analysis options,
  imported types/symbols, mapping and the exporter schema.
- lifecycle (P7.5): the batched exporter already reuses one ``DecompInterface`` per imported Program and
  disposes it in a ``finally``; :class:`GhidraSession`/`SessionManager` model the same contract for future
  long-lived sessions (bounded, idly evicted, process-group cleanup), and map ``flushCache()`` metadata
  changes vs the stronger reset/reimport for invalidated Program views (overlays) onto cache invalidation.
- proposals and alternatives (P7.6/P7.7): analysis observations become :class:`AnalysisProposal` records
  that can only be applied through decomp.wtf's protected edit path (never by direct mutation), and a
  :class:`DraftPortfolio` keeps m2c as the matching-oriented primary route without demoting a verified best
  merely because Ghidra C is easier to read.

``decompile_object`` remains as a compatibility projection (name -> C) for existing C-draft callers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path

from .. import scheduler, toolchains

#: Exporter/schema version: the reviewed exporter (schema 1) emitted only {"name", "c"} rows. Schema 2 adds
#: per-function status/coverage fields and a separate <out>.summary.json with import-level completion metadata.
#: Bumped: cache entries and rows from older schemas are identity-incomplete and are never reused.
EXPORTER_SCHEMA = 2
FACETS = ("pseudocode", "instructions", "blocks", "calls", "references", "types", "pcode")
#: Unknown (not requested), unavailable (the provider cannot supply it here), empty (computed, nothing
#: there), failed (attempted, errored), partial and ok are different values.
FACET_STATUSES = ("unknown", "ok", "partial", "failed", "unavailable", "empty")
DEFAULT_TIMEOUT = 900.0


class GhidraError(RuntimeError):
    pass


class GhidraUnavailable(GhidraError):
    """Ghidra (analyzeHeadless) or Java 21+ is not installed on this host."""


class AdmissionError(GhidraError):
    """A target profile was rejected before any analysis ran; the message lists the reasons."""


class ProposalError(GhidraError):
    """An analysis proposal tried to mutate project state directly (only the protected edit path may)."""


# Workspace-local runtimes (Workstream C): Java/Ghidra discovery never touches system directories.
# Order: DECOMP_JAVA_HOME / DECOMP_GHIDRA_HOME, then the workspace provisioning, then PATH/registry.
_WORKSPACE_JAVA_DIR = Path.home() / "Decomp" / "java"
_WORKSPACE_GHIDRA_DIRS = (
    Path.home() / "Documents" / "Codex" / "2026-10-14" / "i-want" / "work" / "ghidra-displaylink"
    / "ghidra_12.1.4_PUBLIC",
)


def _java_bin(home: Path) -> Path | None:
    exe = home / "bin" / "java"
    if exe.is_file():
        return exe
    mac = home / "Contents" / "Home" / "bin" / "java"  # macOS JRE layout
    return mac if mac.is_file() else None


def java_home() -> Path | None:
    """A Java runtime root (the directory containing bin/java): DECOMP_JAVA_HOME, the workspace
    ~/Decomp/java/*/Contents/Home layout, then PATH. Returns the normalised home; never installs or
    touches system directories."""
    candidates: list[Path] = []
    env = os.environ.get("DECOMP_JAVA_HOME")
    if env:
        candidates.append(Path(env))
    if _WORKSPACE_JAVA_DIR.is_dir():
        for entry in sorted(_WORKSPACE_JAVA_DIR.iterdir()):
            if entry.is_dir():
                candidates.append(entry)
    exe = shutil.which("java")
    if exe:
        p = Path(exe).resolve()
        parent = p.parent
        candidates.append(parent if parent.name == "bin" else parent.parent)
    for candidate in candidates:
        exe = candidate / "bin" / "java"
        if exe.is_file():
            return candidate
        mac = candidate / "Contents" / "Home"  # macOS JRE layout
        if (mac / "bin" / "java").is_file():
            return mac
    return None


def java_version() -> int:
    """Major version of the discovered java (java_home() first, then PATH), 0 if none."""
    exe = None
    home = java_home()
    if home is not None:
        exe = _java_bin(home)
    if exe is None:
        found = shutil.which("java")
        exe = Path(found) if found else None
    if exe is None:
        return 0
    try:
        out = subprocess.run([str(exe), "-version"], capture_output=True, text=True, timeout=20,
                             encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return 0
    m = re.search(r'version "(\d+)', out.stderr + out.stdout)
    return int(m.group(1)) if m else 0


def ghidra_home() -> Path | None:
    """The Ghidra install whose support/analyzeHeadless exists: DECOMP_GHIDRA_HOME, the decompnet
    toolchain registry, or the pinned workspace install (read-only)."""
    launcher = "analyzeHeadless"
    candidates: list[Path] = []
    env = os.environ.get("DECOMP_GHIDRA_HOME")
    if env:
        candidates.append(Path(env))
    candidates.append(toolchains.path("ghidra"))
    candidates.extend(_WORKSPACE_GHIDRA_DIRS)
    for candidate in candidates:
        if (candidate / "support" / launcher).is_file():
            return candidate
    return None


def available() -> bool:
    return ghidra_home() is not None and java_home() is not None and java_version() >= 21


def _headless() -> Path:
    home = ghidra_home()
    if home is not None:
        return home / "support" / "analyzeHeadless"
    return toolchains.path("ghidra") / "support" / "analyzeHeadless"


def provider_version() -> str:
    """The pinned Ghidra release the headless launcher belongs to ('ghidra-12.1.4'), or 'ghidra-unknown'."""
    manifest = toolchains.installed("ghidra")
    if manifest:
        for part in manifest.get("parts") or []:
            hit = re.search(r"ghidra[_-](\d+\.\d+(?:\.\d+)?)", str(part.get("url", "")))
            if hit:
                return f"ghidra-{hit.group(1)}"
    try:
        hit = re.search(r"ghidra[_-](\d+\.\d+(?:\.\d+)?)", _headless().resolve().parent.parent.name)
        if hit:
            return f"ghidra-{hit.group(1)}"
    except OSError:
        pass
    return "ghidra-unknown"


def exporter_source() -> Path:
    """The Java exporter shipped with this module (read by tests and copied into the headless script dir)."""
    return resources.files("decompnet.forks").joinpath("ghidra_scripts", "DecompWtfExport.java")


# ---------------------------------------------------------------------------- coordinates and profiles

@dataclass(frozen=True)
class Coordinates:
    """One function's place in four separate coordinate systems (P7.1.5): file offsets, original virtual
    addresses, analysis (Ghidra address space) addresses and object-section offsets never collapse into one
    number."""

    module: str = ""
    section: str = ""
    space: str = ""
    file_offset: int | None = None
    section_offset: int | None = None
    vaddr: int | None = None
    analysis_address: int | None = None

    def key(self) -> str:
        """Canonical identity: module + analysis space + section + analysis address. Names are labels, not
        identity: the same name/address in another module or overlay stays a distinct function."""
        return "\x1f".join((self.module, self.space, self.section, str(self.analysis_address)))


@dataclass(frozen=True)
class MemoryRegion:
    """A mapped region from the platform extractor (the authority on where bytes live)."""

    name: str
    file_offset: int
    size: int
    vaddr: int
    analysis_base: int | None = None  # analysis address of vaddr; defaults to vaddr
    space: str = "ram"
    is_overlay: bool = False

    def base(self) -> int:
        return self.vaddr if self.analysis_base is None else self.analysis_base


@dataclass(frozen=True)
class Symbol:
    """An authoritative symbol from the extractor (label, kind, original address)."""

    name: str
    kind: str
    vaddr: int
    size: int = 0


@dataclass(frozen=True)
class TargetProfile:
    """What is being analysed and how. Ghidra language/compiler-spec/loader/analysis options are selected
    and pinned at admission time, not guessed per run."""

    kind: str  # object | executable | raw_region
    processor: str = ""  # mips, powerpc, arm, x86, aarch64, sh, ...
    bits: int = 0
    endian: str = ""  # big | little
    mode: str = ""  # arm | thumb | ... (required for ARM)
    compiler_spec: str = ""  # pinned Ghidra compiler-spec id ("" = the language default)
    loader: str = ""  # pinned loader ("" = auto / chosen from the artifact format)
    analysis_options: tuple = ()
    memory_map: tuple = ()
    symbols: tuple = ()
    boundaries: tuple = ()  # authoritative function extents as (vaddr_start, vaddr_end)
    metadata: dict = field(default_factory=dict)  # imported types/symbols revision, etc. (cache identity)


# (processor, bits, endian[, mode]) -> Ghidra language id. Combinations absent from this table are rejected
# with an explicit reason rather than guessed. Validate against the pinned release before enabling more.
_LANGUAGES = {
    ("mips", 32, "big"): "MIPS:BE:32:default",
    ("mips", 32, "little"): "MIPS:LE:32:default",
    ("mips", 64, "big"): "MIPS:BE:64:64-32addr",
    ("mips", 64, "little"): "MIPS:LE:64:64-32addr",
    ("powerpc", 32, "big"): "PowerPC:BE:32:default",
    ("powerpc", 64, "big"): "PowerPC:BE:64:default",
    ("arm", 32, "little", "arm"): "ARM:LE:32:v7",
    ("arm", 32, "little", "thumb"): "ARM:LE:32:v7T",
    ("arm", 32, "big", "arm"): "ARM:BE:32:v7",
    ("arm", 32, "big", "thumb"): "ARM:BE:32:v7T",
    ("aarch64", 64, "little"): "AARCH64:LE:64:v8A",
    ("x86", 32, "little"): "x86:LE:32:default",
    ("x86", 64, "little"): "x86:LE:64:default",
    ("sh", 32, "little"): "SuperH4:LE:32:default",
}
_LOADERS = ("Elf", "Pe", "COFF", "Mach-O", "Raw Binary")
#: Internal loader ids -> the loader CLASS names analyzeHeadless -loader accepts (HeadlessOptions
#: resolves via LoaderService.getLoaderClassByName; verified against the pinned 12.1.4 loaders).
_LOADER_NAMES = {
    "Elf": "ElfLoader",
    "Pe": "PeLoader",
    "COFF": "CoffLoader",
    "Mach-O": "MachoLoader",
    "Raw Binary": "BinaryLoader",
}


def elf_info(data: bytes) -> dict | None:
    """The ELF identification (class, data encoding, file type) of `data`, or None when it isn't ELF."""
    if len(data) < 20 or data[:4] != b"\x7fELF":
        return None
    cls, enc = data[4], data[5]
    if cls not in (1, 2) or enc not in (1, 2):
        return {"format": "elf", "class": cls, "encoding": enc, "type": "unknown"}
    etype = int.from_bytes(data[16:18], "little")
    return {
        "format": "elf",
        "class": 32 if cls == 1 else 64,
        "encoding": "little" if enc == 1 else "big",
        "type": {1: "object", 2: "executable", 3: "executable"}.get(etype, "unknown"),
    }


def _coerce_profile(profile: TargetProfile | dict | None) -> TargetProfile | None:
    if profile is None or isinstance(profile, TargetProfile):
        return profile
    if not isinstance(profile, dict):
        raise AdmissionError(f"profile must be a TargetProfile or dict, not {type(profile).__name__}")
    fields = {f.name for f in TargetProfile.__dataclass_fields__.values()}
    unknown = sorted(set(profile) - fields)
    if unknown:
        raise AdmissionError(f"unknown profile field(s): {', '.join(unknown)}")
    return TargetProfile(**profile)


def _coerce_regions(profile: TargetProfile) -> tuple[MemoryRegion, ...]:
    out = []
    for r in profile.memory_map:
        if isinstance(r, MemoryRegion):
            out.append(r)
        elif isinstance(r, dict):
            fields = {f.name for f in MemoryRegion.__dataclass_fields__.values()}
            unknown = sorted(set(r) - fields)
            if unknown:
                raise AdmissionError(f"unknown memory-region field(s): {', '.join(unknown)}")
            out.append(MemoryRegion(**r))
        else:
            raise AdmissionError(f"memory map entry {r!r} is not a MemoryRegion")
    return tuple(out)


def resolve_language(profile: TargetProfile) -> tuple[str | None, str | None, str | None]:
    """The pinned Ghidra (language, compiler spec, loader) for a profile, or an explicit AdmissionError."""
    processor, bits, endian, mode = (profile.processor.lower(), profile.bits, profile.endian.lower(),
                                     profile.mode.lower())
    if processor == "arm" and mode not in ("arm", "thumb"):
        raise AdmissionError("ARM profiles must pin the processor mode ('arm' or 'thumb'): instruction "
                             "decoding, literal pools and interworking depend on it")
    key = (processor, bits, endian) if processor != "arm" else (processor, bits, endian, mode)
    language = _LANGUAGES.get(key)
    if language is None:
        supported = ", ".join(f"{p}/{b}-bit/{e}" + (f" ({m})" if p == "arm" else "")
                              for (p, b, e, *m) in sorted(_LANGUAGES))
        raise AdmissionError(
            f"unsupported Ghidra language for {profile.processor or '?'}/{profile.bits}-bit/"
            f"{profile.endian or '?'}{(' in ' + profile.mode + ' mode') if profile.mode else ''}; supported "
            f"combinations: {supported}")
    return language, None, None  # compiler spec None = the language's own default (never guessed per language)


def admit(profile: TargetProfile | dict | None, data: bytes):
    """Validate a target profile against the canonical artifact bytes and pin language/compiler-spec/loader.

    A None profile is *auto* mode for the compatibility path: the ELF header (when present) still identifies
    class/encoding/type, the manifest records the auto identity, and Ghidra picks the language itself. Auto
    mode is marked as such - it is never a silent guess presented as a pinned profile.
    """
    profile = _coerce_profile(profile)
    info = elf_info(data) or {}
    if profile is None:
        kind = info.get("type", "object")
        warnings = ("auto admission: no pinned Ghidra language/compiler-spec/loader; the provider chooses "
                    "them and the manifest records only what it reports",)
        return AdmittedProfile(
            kind=kind, processor="", bits=info.get("class", 0), endian=info.get("encoding", ""), mode="",
            language_id=None, compiler_spec=None, loader=None, analysis_options=(), memory_map=(),
            symbols=(), boundaries=(), metadata=(), auto=True, detected=info, warnings=warnings)

    if profile.kind not in ("object", "executable", "raw_region"):
        raise AdmissionError(f"unsupported target kind {profile.kind!r}: expected object, executable or raw_region")
    regions = _coerce_regions(profile)
    if profile.kind == "raw_region" and not regions:
        raise AdmissionError("a raw_region profile needs the extractor's memory map: without base addresses "
                             "the bytes cannot be decoded into the right coordinates")
    for r in regions:
        if r.file_offset < 0 or r.size <= 0 or r.file_offset + r.size > len(data):
            raise AdmissionError(f"memory region {r.name!r} [{r.file_offset:#x}, +{r.size:#x}) lies outside "
                                 f"the {len(data)}-byte artifact")
    for i in range(len(regions)):
        for j in range(i + 1, len(regions)):
            a, b = regions[i], regions[j]
            if a.space == b.space and not (a.is_overlay and b.is_overlay):
                if a.base() < b.base() + b.size and b.base() < a.base() + a.size:
                    raise AdmissionError(f"memory regions {a.name!r} and {b.name!r} overlap in address space "
                                         f"{a.space!r}: {a.base():#x} and {b.base():#x} collapse onto the same "
                                         f"addresses (mark overlays with is_overlay)")
    if info and profile.kind in ("object", "executable"):
        if info.get("encoding") and info["encoding"] != profile.endian:
            raise AdmissionError(f"the artifact is {info['encoding']}-endian ELF but the profile says "
                                 f"{profile.endian or 'nothing'}: wrong-endian profiles cannot decode it")
        if info.get("class") and info["class"] != profile.bits:
            raise AdmissionError(f"the artifact is ELF{info['class']} but the profile says {profile.bits}-bit")
        if info.get("type") and info["type"] != "unknown" and profile.kind == "object" and info["type"] != "object":
            raise AdmissionError(f"the artifact is a linked {info['type']} ({int.from_bytes(data[16:18], 'little')} "
                                 f"e_type), not a relocatable object: import it with kind={info['type']!r}")
        if info.get("type") and info["type"] != "unknown" and profile.kind == "executable" and info["type"] == "object":
            raise AdmissionError("the artifact is a relocatable object (ET_REL), not a linked executable")
    language, cspec, loader = resolve_language(profile)
    if profile.loader:
        if profile.loader not in _LOADERS:
            raise AdmissionError(f"unknown loader {profile.loader!r}; supported: {', '.join(_LOADERS)}")
        loader = profile.loader
    elif profile.kind == "raw_region":
        loader = "Raw Binary"
    elif info.get("format") == "elf":
        loader = "Elf"
    if profile.compiler_spec:
        cspec = profile.compiler_spec
    boundaries = []
    for b in profile.boundaries:
        if not (isinstance(b, (tuple, list)) and len(b) == 2 and b[1] > b[0]):
            raise AdmissionError(f"function boundary {b!r} is not a (start, end) extent")
        boundaries.append((int(b[0]), int(b[1])))
    boundaries = tuple(boundaries)
    if boundaries and regions:
        for start, end in boundaries:
            if not any(r.vaddr <= start < end <= r.vaddr + r.size for r in regions):
                raise AdmissionError(f"authoritative boundary [{start:#x}, {end:#x}) does not lie inside any "
                                     f"memory region: the extractor's map and boundaries disagree")
    warnings = []
    if not regions:
        warnings.append("no memory map provided: file-offset/vaddr coordinates will be unknown")
    return AdmittedProfile(
        kind=profile.kind, processor=profile.processor, bits=profile.bits, endian=profile.endian,
        mode=profile.mode, language_id=language, compiler_spec=cspec, loader=loader,
        analysis_options=tuple(profile.analysis_options), memory_map=regions,
        symbols=tuple(profile.symbols), boundaries=boundaries,
        metadata=tuple(sorted(profile.metadata.items())), auto=False, detected=info,
        warnings=tuple(warnings))


@dataclass(frozen=True)
class AdmittedProfile:
    """A validated, pinned profile ready to run (or record in cache identity)."""

    kind: str
    processor: str
    bits: int
    endian: str
    mode: str
    language_id: str | None
    compiler_spec: str | None
    loader: str | None
    analysis_options: tuple
    memory_map: tuple
    symbols: tuple
    boundaries: tuple
    metadata: tuple
    auto: bool
    detected: dict
    warnings: tuple = ()

    def identity(self) -> dict:
        """Everything material to analysis identity, JSON-serialisable for the cache key (P7.4.7)."""
        return {
            "kind": self.kind, "processor": self.processor, "bits": self.bits, "endian": self.endian,
            "mode": self.mode, "language_id": self.language_id, "compiler_spec": self.compiler_spec,
            "loader": self.loader, "analysis_options": list(self.analysis_options),
            "memory_map": [asdict(r) for r in self.memory_map],
            "symbols": [asdict(s) for s in self.symbols],
            "boundaries": [list(b) for b in self.boundaries],
            "metadata": [list(kv) for kv in self.metadata],
            "auto": self.auto, "detected": self.detected,
        }

    def script_args(self) -> list[str]:
        args = []
        if self.language_id:
            args += ["-processor", self.language_id]
        if self.compiler_spec:  # only pass a compiler spec when the caller pinned one: -cspec ids are
            args += ["-cspec", self.compiler_spec]  # per-language (e.g. x86:LE:64:default has no "default")
        if self.loader:
            args += ["-loader", _LOADER_NAMES.get(self.loader, self.loader)]
        return args


# ---------------------------------------------------------------------------- observations and dossiers

@dataclass
class Pseudocode:
    status: str = "unknown"  # ok | failed | unavailable | unknown
    text: str = ""
    warnings: list = field(default_factory=list)
    limits: list = field(default_factory=list)


@dataclass
class InstructionObservation:
    address: int
    size: int = 0
    mnemonic: str = ""
    operands: str = ""
    bytes_hex: str = ""  # present only when the caller requested original bytes


@dataclass
class BlockObservation:
    start: int
    end: int
    successors: list = field(default_factory=list)


@dataclass
class CallObservation:
    address: int
    target: int | None = None
    name: str = ""
    indirect: bool = False


@dataclass
class ReferenceObservation:
    """A data reference, string or literal observation at `address`, with its coordinates in `target`."""

    address: int
    kind: str = "data"  # data | string | literal
    target: int | None = None
    value: str = ""


@dataclass
class TypeObservation:
    """A type present in the analysis database. origin='decompiler' marks it as recovered, not original
    source truth (R02: a Ghidra compiler spec is not compiler identification)."""

    name: str
    signature: str
    origin: str = "decompiler"
    complete: bool = False


@dataclass
class PcodeObservation:
    status: str = "unknown"  # ok | partial | failed | unavailable | unknown
    limit: int = 0
    truncated: bool = False
    ops: list = field(default_factory=list)


@dataclass
class FunctionDossier:
    """Everything the exporter observed about one function, keyed by canonical coordinates."""

    key: str
    name: str
    namespace: str = ""
    module: str = ""
    section: str = ""
    space: str = ""
    entry: int | None = None
    block_offset: int | None = None
    file_offset: int | None = None
    section_offset: int | None = None
    vaddr: int | None = None
    body_ranges: list = field(default_factory=list)  # (analysis_start, analysis_end) extents, noncontiguous ok
    classification: str = "unknown"  # normal | external | thunk | unknown
    thunk_of: int | None = None
    boundary_status: str = "unknown"  # exact | entry-only | proposed | unknown (vs the extractor's boundaries)
    pseudocode: Pseudocode = field(default_factory=Pseudocode)
    instructions: list = field(default_factory=list)
    blocks: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    references: list = field(default_factory=list)
    types: list = field(default_factory=list)
    pcode: PcodeObservation | None = None
    coverage: dict = field(default_factory=dict)  # facet -> status
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FunctionDossier":
        pcode = d.get("pcode")
        return cls(
            key=d.get("key", ""), name=d.get("name", ""), namespace=d.get("namespace", ""),
            module=d.get("module", ""), section=d.get("section", ""), space=d.get("space", ""),
            entry=d.get("entry"), block_offset=d.get("block_offset"), file_offset=d.get("file_offset"),
            section_offset=d.get("section_offset"), vaddr=d.get("vaddr"),
            body_ranges=[tuple(r) for r in d.get("body_ranges") or []],
            classification=d.get("classification", "unknown"), thunk_of=d.get("thunk_of"),
            boundary_status=d.get("boundary_status", "unknown"),
            pseudocode=Pseudocode(**(d.get("pseudocode") or {})),
            instructions=[InstructionObservation(**i) for i in d.get("instructions") or []],
            blocks=[BlockObservation(**b) for b in d.get("blocks") or []],
            calls=[CallObservation(**c) for c in d.get("calls") or []],
            references=[ReferenceObservation(**r) for r in d.get("references") or []],
            types=[TypeObservation(**t) for t in d.get("types") or []],
            pcode=PcodeObservation(**pcode) if pcode else None,
            coverage=dict(d.get("coverage") or {}),
            warnings=list(d.get("warnings") or []),
        )

    @property
    def noncontiguous(self) -> bool:
        return len(self.body_ranges) > 1


# ---------------------------------------------------------------------------- manifest and cache

@dataclass
class AnalysisManifest:
    """One import's complete record: import-level status, per-facet coverage and per-function dossiers."""

    schema: int
    cache_key: str
    input_digest: str
    provider: str
    provider_version: str
    exporter: str
    profile: dict
    status: str  # complete | partial | failed | timeout | cancelled
    functions: dict = field(default_factory=dict)  # canonical key -> FunctionDossier
    coverage: dict = field(default_factory=dict)  # facet -> status -> count
    functions_total: int | None = None
    functions_exported: int = 0
    missing_functions: list = field(default_factory=list)
    exit_code: int | None = None
    timed_out: bool = False
    cancel_requested: bool = False
    diagnostics: list = field(default_factory=list)
    language_id: str | None = None
    compiler_spec: str | None = None
    loader: str | None = None
    from_cache: bool = False
    cache_path: Path | None = None

    def is_complete(self) -> bool:
        return self.status == "complete"

    def usable(self) -> bool:
        """Valid evidence with known coverage: complete, or partial with explicit missing-function records."""
        return self.status == "complete" or (self.status == "partial" and self.functions_exported > 0)

    def names(self) -> dict[str, str]:
        """Compatibility projection for existing C-draft callers: name -> decompiled C for every function with
        a successful pseudocode facet. Duplicate names keep the last dossier, as the reviewed exporter did."""
        out: dict[str, str] = {}
        for d in self.functions.values():
            if d.pseudocode.status == "ok" and d.pseudocode.text:
                out[d.name] = d.pseudocode.text
        return out

    def dossiers(self, classification: str | None = None) -> list[FunctionDossier]:
        rows = list(self.functions.values())
        return [d for d in rows if classification is None or d.classification == classification] if classification else rows

    def by_name(self, name: str) -> FunctionDossier | None:
        return next((d for d in self.functions.values() if d.name == name), None)

    def coverage_for(self, facet: str) -> dict[str, int]:
        return dict(self.coverage.get(facet) or {})

    def to_dict(self) -> dict:
        return {
            "schema": self.schema, "cache_key": self.cache_key, "input_digest": self.input_digest,
            "provider": self.provider, "provider_version": self.provider_version, "exporter": self.exporter,
            "profile": self.profile, "status": self.status, "coverage": self.coverage,
            "functions_total": self.functions_total, "functions_exported": self.functions_exported,
            "missing_functions": self.missing_functions, "exit_code": self.exit_code,
            "timed_out": self.timed_out, "cancel_requested": self.cancel_requested,
            "diagnostics": self.diagnostics, "language_id": self.language_id,
            "compiler_spec": self.compiler_spec, "loader": self.loader,
            "functions": {k: v.to_dict() for k, v in self.functions.items()},
        }


def invalidate(cache_dir: Path, stem: str | None = None) -> int:
    """Drop cached manifests for one object stem (or everything, when stem is None). A metadata change
    (types/symbols/analysis options/mapping) is part of the cache identity, so entries computed under the
    previous identity must not be reused; overlay/view changes additionally require a re-import, which the
    next acquisition performs. Returns the number of files removed."""
    if not cache_dir.exists():
        return 0
    pattern = "*" if stem is None else f"{stem}-*"
    removed = 0
    for p in cache_dir.glob(pattern):
        if p.is_file():
            p.unlink()
            removed += 1
    return removed


def _cache_key(obj: Path, digest: str, admitted: AdmittedProfile, facets: tuple, want_bytes: bool) -> str:
    payload = {
        "v": EXPORTER_SCHEMA,
        "stem": obj.name,
        "digest": digest,
        "profile": admitted.identity(),
        "ghidra": provider_version(),
        "facets": list(facets),
        "bytes": bool(want_bytes),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _load_manifest(path: Path, key: str, digest: str) -> AnalysisManifest | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        path.unlink(missing_ok=True)
        return None
    if (not isinstance(raw, dict) or raw.get("schema") != EXPORTER_SCHEMA or raw.get("cache_key") != key
            or raw.get("input_digest") != digest):
        path.unlink(missing_ok=True)  # identity/schema mismatch: quarantined by removal, recomputed fresh
        return None
    try:
        m = AnalysisManifest(
            schema=EXPORTER_SCHEMA, cache_key=raw["cache_key"], input_digest=raw["input_digest"],
            provider=raw.get("provider", "ghidra-headless"),
            provider_version=raw.get("provider_version", "ghidra-unknown"),
            exporter=raw.get("exporter", "DecompWtfExport.java"), profile=raw.get("profile") or {},
            status=raw.get("status", "failed"), coverage=dict(raw.get("coverage") or {}),
            functions_total=raw.get("functions_total"), functions_exported=int(raw.get("functions_exported", 0)),
            missing_functions=list(raw.get("missing_functions") or []), exit_code=raw.get("exit_code"),
            timed_out=bool(raw.get("timed_out")), cancel_requested=bool(raw.get("cancel_requested")),
            diagnostics=list(raw.get("diagnostics") or []), language_id=raw.get("language_id"),
            compiler_spec=raw.get("compiler_spec"), loader=raw.get("loader"),
            from_cache=True, cache_path=path)
        for k, v in (raw.get("functions") or {}).items():
            m.functions[k] = FunctionDossier.from_dict(v)
        return m
    except Exception:
        path.unlink(missing_ok=True)
        return None


def _write_manifest(path: Path, manifest: AnalysisManifest) -> None:
    """Publish atomically: a crash mid-write leaves the previous entry (or nothing), never a torn file."""
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    os.replace(tmp, path)
    manifest.cache_path = path


# ---------------------------------------------------------------------------- headless subprocess

@dataclass
class _RunResult:
    status: str
    exit_code: int | None = None
    timed_out: bool = False
    cancel_requested: bool = False
    diagnostics: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    summary: dict | None = None


def _facet_args(admitted: AdmittedProfile, facets: tuple, want_bytes: bool) -> list[str]:
    args = []
    if "instructions" in facets:
        args.append("--instructions")
    if want_bytes:
        args.append("--bytes")
    if "blocks" in facets:
        args.append("--blocks")
    if "calls" in facets:
        args.append("--calls")
    if "references" in facets:
        args.append("--refs")
    if "types" in facets:
        args.append("--types")
    if "pcode" in facets:
        args.append("--pcode")
    for opt in admitted.analysis_options:
        args += ["--analysis-option", str(opt)]
    return args


def _kill(proc: subprocess.Popen) -> None:
    """Process-group cleanup: the headless JVM plus anything it spawned (the plan's forced cleanup)."""
    try:
        if proc.pid:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        pass


def _communicate(proc: subprocess.Popen, timeout: float, cancel: threading.Event | None):
    """communicate() bounded by `timeout`, aborting early when `cancel` fires (which also kills the group)."""
    if cancel is None:
        return proc.communicate(timeout=timeout)
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if cancel.is_set():
            _kill(proc)
            return "", ""
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        try:
            return proc.communicate(timeout=0.2)
        except subprocess.TimeoutExpired:
            continue
    return proc.communicate()


def _read_rows(out: Path) -> list[dict]:
    rows = []
    if out.exists():
        for line in out.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("type") == "summary":
                continue  # summaries belong in <out>.summary.json; tolerate one appearing inline
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _read_summary(out: Path) -> dict | None:
    path = Path(str(out) + ".summary.json")
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        return None
    return raw if isinstance(raw, dict) else None


def _run_headless(admitted: AdmittedProfile, obj: Path, scripts_src: Path, facets: tuple, want_bytes: bool,
                  timeout: float, cancel: threading.Event | None, data: bytes | None = None,
                  digest: str | None = None) -> _RunResult:
    """Import the object with headless Ghidra and run the exporter post-script. The exporter reuses one
    DecompInterface for the whole Program, so one import serves every function. Replaced wholesale in tests.

    `data` are the bytes the identity `digest` was computed from: they are written once to the private work
    copy, the copy itself is re-hashed, and the import is refused when it differs from `digest`."""
    if data is None:
        data = obj.read_bytes()
    with tempfile.TemporaryDirectory(prefix="decompwtf-ghidra-") as tmp:
        tmp = Path(tmp)
        scripts = tmp / "scripts"
        scripts.mkdir()
        (scripts / "DecompWtfExport.java").write_text(scripts_src.read_text(encoding="utf-8"), encoding="utf-8")
        out = tmp / "out.jsonl"
        target = tmp / obj.name
        target.write_bytes(data)
        copy_digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest is not None and copy_digest != digest:
            return _RunResult(status="failed", diagnostics=[
                f"identity mismatch: the private work copy of {obj.name} hashes to {copy_digest} but the "
                f"recorded identity is {digest}; nothing was imported or cached"])
        try:
            os.chmod(target, 0o444)  # the imported copy stays what was hashed
        except OSError:
            pass
        argv = [str(_headless()), str(tmp), "decompwtf", "-import", str(target), "-scriptPath", str(scripts),
                "-analysisTimeoutPerFile", "600", *admitted.script_args(), "-postScript",
                "DecompWtfExport.java", str(out), *_facet_args(admitted, facets, want_bytes), "-deleteProject"]
        try:
            env = dict(os.environ)
            jh = java_home()
            if jh is not None:  # the launcher bootstraps from JAVA_HOME when PATH lacks a real java
                env["JAVA_HOME"] = str(jh)
            # DECOMP_GHIDRA_USER_HOME relocates Ghidra's user state (logs/preferences/caches, which
            # default to ~/Library/ghidra on macOS) — needed by sandboxed/CI runs that may only write
            # inside the workspace; unset keeps Ghidra's normal layout. JAVA_TOOL_OPTIONS reaches
            # every JVM the launcher spawns, including the LaunchSupport step that persists
            # java_home.save BEFORE GHIDRA_JAVA_OPTIONS is applied.
            if os.environ.get("DECOMP_GHIDRA_USER_HOME"):
                extra = os.environ.get("GHIDRA_JAVA_OPTIONS", "")
                env["GHIDRA_JAVA_OPTIONS"] = (extra + f" -Duser.home={os.environ['DECOMP_GHIDRA_USER_HOME']}").strip()
                tool_opts = os.environ.get("JAVA_TOOL_OPTIONS", "")
                env["JAVA_TOOL_OPTIONS"] = (tool_opts + f" -Duser.home={os.environ['DECOMP_GHIDRA_USER_HOME']}").strip()
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", start_new_session=True,
                                    env=env)
        except OSError as e:
            return _RunResult(status="failed", diagnostics=[f"analyzeHeadless failed to start: {e}"])
        try:
            _communicate(proc, timeout, cancel)
        except subprocess.TimeoutExpired:
            _kill(proc)
            return _RunResult(status="timeout", timed_out=True, exit_code=proc.poll(),
                              rows=_read_rows(out), summary=_read_summary(out),
                              diagnostics=[f"analyzeHeadless exceeded {timeout:g}s; its process group was killed"])
        except OSError as e:
            _kill(proc)
            return _RunResult(status="failed", exit_code=proc.poll(), diagnostics=[f"headless process failed: {e}"])
        exit_code = proc.returncode
        if cancel is not None and cancel.is_set():
            _kill(proc)
            return _RunResult(status="cancelled", cancel_requested=True, exit_code=proc.poll(),
                              rows=_read_rows(out), summary=_read_summary(out),
                              diagnostics=["cancelled by the caller; no cache entry was published"])
        rows, summary = _read_rows(out), _read_summary(out)
        diagnostics = []
        if summary:
            st = summary.get("status")
            if st == "cancelled":
                return _RunResult(status="cancelled", cancel_requested=True, exit_code=exit_code, rows=rows,
                                  summary=summary, diagnostics=["the exporter was cancelled (monitor)"])
            if st == "partial":
                return _RunResult(status="partial", exit_code=exit_code, rows=rows, summary=summary,
                                  diagnostics=[f"the exporter reports partial coverage: "
                                               f"{summary.get('functions_exported')} of "
                                               f"{summary.get('functions_total')} functions exported"])
            if st == "complete":
                return _RunResult(status="complete", exit_code=exit_code, rows=rows, summary=summary,
                                  diagnostics=diagnostics)
        if exit_code == 0:
            if rows:
                return _RunResult(status="complete", exit_code=0, rows=rows, summary=summary,
                                  diagnostics=["no summary file: rows-only exporter output"])
            return _RunResult(status="failed", exit_code=0, diagnostics=[
                "analyzeHeadless exited 0 without export rows or a summary: nothing to reuse, nothing cached"])
        if rows:
            return _RunResult(status="partial", exit_code=exit_code, rows=rows, summary=summary,
                              diagnostics=[f"analyzeHeadless exited {exit_code} after writing {len(rows)} rows: "
                                           f"partial coverage retained, completion unknown"])
        return _RunResult(status="failed", exit_code=exit_code,
                          diagnostics=[f"analyzeHeadless exited {exit_code} without exporting anything"])


# ---------------------------------------------------------------------------- analysis entry points

def _region_for(regions: tuple, space: str, entry: int | None) -> MemoryRegion | None:
    if entry is None:
        return None
    exact = [r for r in regions if r.space == space and r.base() <= entry < r.base() + r.size]
    if exact:
        return exact[0]
    return next((r for r in regions if r.base() <= entry < r.base() + r.size), None)


def _boundary_status(boundaries: tuple, entry_vaddr: int | None, vaddr_ranges: list) -> str:
    if not boundaries or entry_vaddr is None:
        return "unknown"
    for start, end in boundaries:
        if start <= entry_vaddr < end:
            if len(vaddr_ranges) == 1 and vaddr_ranges[0] == (start, end):
                return "exact"
            return "entry-only"
    return "proposed"


def _parse_row(row: dict, obj: Path, admitted: AdmittedProfile, wants: set) -> FunctionDossier:
    name = str(row.get("name") or "")
    module = str(row.get("module") or obj.name)
    classification = str(row.get("classification") or ("unknown" if "classification" not in row else "normal"))
    entry = row.get("entry")
    space = str(row.get("space") or "")
    section = str(row.get("section") or "")
    block_offset = row.get("block_offset")
    ranges = []
    for r in row.get("ranges") or []:
        if isinstance(r, (list, tuple)) and len(r) == 2:
            ranges.append((int(r[0]), int(r[1])))
    pseudo_raw = row.get("pseudocode") or {}
    c = str(row.get("c") or "")
    if classification in ("thunk", "external"):
        pstatus, text = "unavailable", c
    elif pseudo_raw.get("status"):
        pstatus, text = str(pseudo_raw["status"]), c
    elif c:
        pstatus, text = "ok", c
    elif "c" in row:
        pstatus, text = "failed", ""
    else:
        pstatus, text = "unknown", ""
    cov = {facet: "unknown" for facet in FACETS}
    for facet in FACETS:
        st = str((row.get("coverage") or {}).get(facet) or "unknown")
        cov[facet] = st if st in FACET_STATUSES else "unknown"
    cov["pseudocode"] = pstatus if pstatus in FACET_STATUSES else "unknown"
    file_offset = section_offset = vaddr = None
    region = _region_for(admitted.memory_map, space, entry)
    if region is not None and entry is not None:
        base = region.base()
        if base <= int(entry) < base + region.size:
            delta = int(entry) - base
            file_offset, section_offset, vaddr = region.file_offset + delta, delta, region.vaddr + delta
    mapped_ranges = ranges if region is not None and region.base() == region.vaddr else []
    pcode = None
    if row.get("pcode"):
        pr = row["pcode"]
        pcode = PcodeObservation(status=str(pr.get("status") or "unknown"), limit=int(pr.get("limit") or 0),
                                 truncated=bool(pr.get("truncated")), ops=[dict(o) for o in pr.get("ops") or []])
    key = (Coordinates(module=module, section=section, space=space, analysis_address=int(entry)).key()
           if entry is not None else "\x1f".join((module, "", "", name)))
    return FunctionDossier(
        key=key, name=name, namespace=str(row.get("namespace") or ""), module=module, section=section, space=space,
        entry=int(entry) if entry is not None else None,
        block_offset=int(block_offset) if block_offset is not None else None,
        file_offset=file_offset, section_offset=section_offset, vaddr=vaddr, body_ranges=ranges,
        classification=classification, thunk_of=row.get("thunk_of"),
        boundary_status=_boundary_status(admitted.boundaries, vaddr, mapped_ranges),
        pseudocode=Pseudocode(status=pstatus, text=text,
                              warnings=[str(w) for w in (pseudo_raw.get("warnings") or [])]),
        instructions=[InstructionObservation(address=int(i["address"]), size=int(i.get("size", 0)),
                                             mnemonic=str(i.get("asm") or ""),
                                             bytes_hex=str(i.get("bytes") or ""))
                      for i in row.get("instructions") or []
                      if isinstance(i, dict) and isinstance(i.get("address"), (int, float))],
        blocks=[BlockObservation(start=int(b["start"]), end=int(b["end"]),
                                 successors=[int(s) for s in b.get("successors") or []])
                for b in row.get("blocks") or []
                if isinstance(b, dict) and isinstance(b.get("start"), (int, float))],
        calls=[CallObservation(address=int(c["address"]), target=c.get("target"),
                               name=str(c.get("name") or ""), indirect=bool(c.get("indirect")))
               for c in row.get("calls") or []
               if isinstance(c, dict) and isinstance(c.get("address"), (int, float))],
        references=[ReferenceObservation(address=int(r["address"]), kind=str(r.get("kind") or "data"),
                                          target=r.get("target"), value=str(r.get("value") or ""))
                    for r in row.get("refs") or []
                    if isinstance(r, dict) and isinstance(r.get("address"), (int, float))],
        types=[TypeObservation(name=str(t.get("name") or ""), signature=str(t.get("signature") or ""),
                               origin=str(t.get("origin") or "decompiler"), complete=bool(t.get("complete")))
               for t in row.get("types") or [] if isinstance(t, dict)],
        pcode=pcode, coverage=cov)


def _build_manifest(obj: Path, digest: str, key: str, admitted: AdmittedProfile, facets: tuple,
                    result: _RunResult) -> AnalysisManifest:
    functions: dict[str, FunctionDossier] = {}
    for n, row in enumerate(result.rows):
        try:
            d = _parse_row(row, obj, admitted, set(facets))
        except Exception as e:  # a malformed row must not erase the rest of a partial export
            result.diagnostics.append(f"row {n}: unparseable ({e})")
            continue
        functions[d.key] = d
    coverage: dict[str, dict[str, int]] = {}
    for facet in FACETS:
        counts: dict[str, int] = {}
        for d in functions.values():
            st = d.coverage.get(facet, "unknown")
            counts[st] = counts.get(st, 0) + 1
        coverage[facet] = counts
    summary = result.summary or {}
    status = result.status
    language_id = summary.get("language")
    compiler_spec = summary.get("cspec")
    if admitted.language_id and language_id and admitted.language_id != language_id:
        result.diagnostics.append(f"language mismatch: admitted {admitted.language_id!r} but the program "
                                  f"loaded as {language_id!r}")
        if status == "complete":
            status = "partial"
    if admitted.compiler_spec and compiler_spec and admitted.compiler_spec != compiler_spec:
        result.diagnostics.append(f"compiler-spec mismatch: admitted {admitted.compiler_spec!r} but the "
                                  f"program loaded as {compiler_spec!r}")
        if status == "complete":
            status = "partial"
    return AnalysisManifest(
        schema=EXPORTER_SCHEMA, cache_key=key, input_digest=digest, provider="ghidra-headless",
        provider_version=provider_version(), exporter="DecompWtfExport.java", profile=admitted.identity(),
        status=status, functions=functions, coverage=coverage,
        functions_total=summary.get("functions_total"), functions_exported=len(functions),
        missing_functions=[str(x) for x in (summary.get("failed") or [])],
        exit_code=result.exit_code, timed_out=result.timed_out, cancel_requested=result.cancel_requested,
        diagnostics=result.diagnostics, language_id=language_id or admitted.language_id,
        compiler_spec=compiler_spec or admitted.compiler_spec, loader=admitted.loader,
        from_cache=False)


def analyze_object(obj: Path, cache_dir: Path, *, profile: TargetProfile | dict | None = None,
                   timeout: float = DEFAULT_TIMEOUT, facets: tuple = (), want_bytes: bool = False,
                   want_pcode: bool = False, refresh: bool = False,
                   cancel: threading.Event | None = None) -> AnalysisManifest:
    """Admit `obj` under `profile` and return its structured analysis manifest, cached by full identity.

    - `profile`: a :class:`TargetProfile` (validated against the artifact bytes) or None for auto mode.
    - `facets`: extra evidence to request beyond pseudocode, from FACETS.
    - `want_bytes`: include original instruction bytes (only with the instructions facet).
    - `want_pcode`: include bounded p-code (implies the pcode facet).
    - `refresh`: bypass a valid cache entry and re-import (a bounded refresh operation).
    - `cancel`: set this event to abort the run; nothing is cached on cancellation.

    Raises :class:`AdmissionError` for rejected profiles, :class:`GhidraUnavailable` when the tool is not
    installed and no cache entry covers the request, ValueError for unknown facets. Transient failures are
    returned as manifests with status failed/timeout/cancelled and are never cached.
    """
    facets = tuple(sorted(set(facets)))
    unknown = [f for f in facets if f not in FACETS]
    if unknown:
        raise ValueError(f"unknown Ghidra facets {unknown}; available: {', '.join(FACETS)}")
    data = obj.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    admitted = admit(profile, data)
    if want_pcode and "pcode" not in facets:
        facets = tuple(sorted({*facets, "pcode"}))
    key = _cache_key(obj, digest, admitted, facets, want_bytes)
    path = cache_dir / f"{obj.stem}-{key[:24]}.json"
    if not refresh:
        cached = _load_manifest(path, key, digest)
        if cached is not None:
            return cached
    if not available():
        raise GhidraUnavailable("Ghidra (analyzeHeadless) is not installed here (the ghidra toolchain and "
                                "Java 21+ are required): no analysis was run and nothing was cached")
    with scheduler.BUDGET.slot("analysis"):
        result = _run_headless(admitted, obj, exporter_source(), facets, want_bytes, timeout, cancel,
                               data=data, digest=digest)
    manifest = _build_manifest(obj, digest, key, admitted, facets, result)
    cacheable = manifest.status == "complete" or (manifest.status == "partial" and manifest.functions_exported > 0)
    if cacheable:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _write_manifest(path, manifest)
    else:
        manifest.diagnostics.append(f"not cached: a {manifest.status} run is transient and must not be "
                                    f"reused as a successful empty dataset")
    return manifest


def decompile_object(obj: Path, cache_dir: Path, timeout: float = DEFAULT_TIMEOUT) -> dict[str, str]:
    """Compatibility projection of the reviewed adapter: name -> decompiled C for every function with a
    successful pseudocode facet. Returns {} when Ghidra is unavailable or the run failed (as before); the
    richer :func:`analyze_object` is the structured entry point."""
    try:
        manifest = analyze_object(obj, cache_dir, timeout=timeout)
    except (GhidraUnavailable, AdmissionError, OSError, ValueError):
        return {}
    return manifest.names()


# ---------------------------------------------------------------------------- lifecycle (P7.5)

class GhidraSession:
    """One persistent decompiler session bound to one owned Program.

    The batched exporter already keeps a single ``DecompInterface`` per imported Program and disposes it in a
    ``finally`` (DecompWtfExport.java); this models the same contract for future long-lived sessions and is
    not wired into ForkEngine yet. In the batched model the cached manifest is the unit of reuse, so
    ``flush_cache``/``reset_reimport`` map onto targeted cache invalidation.
    """

    def __init__(self, key: str, manager: "SessionManager"):
        self.key = key
        self.manager = manager
        self.created_at = time.monotonic()
        self.last_used = self.created_at
        self.disposed = False
        self.diagnostics: list[str] = []

    def _check(self) -> None:
        if self.disposed:
            raise GhidraError(f"session {self.key!r} is disposed")

    def touch(self) -> None:
        self._check()
        self.last_used = time.monotonic()

    def flush_cache(self, *, what: str = "functions") -> str:
        """Function/symbol/type observations changed: drop function-level cached evidence for this target
        (the batched model invalidates the target's manifests - the cache identity already covers the change)."""
        self._check()
        n = self.manager.invalidate_target(self.key)
        message = f"flushCache({what}): {n} cached manifest(s) invalidated"
        self.diagnostics.append(message)
        return message

    def reset_reimport(self, reason: str) -> str:
        """The Program view itself is invalidated (overlay/map change): stronger than flushCache. Cached
        manifests for the target are dropped and this session is disposed; the next acquisition re-imports."""
        self._check()
        n = self.manager.invalidate_target(self.key)
        self.dispose()
        return f"reset/reimport ({reason}): {n} cached manifest(s) invalidated, session disposed"

    def dispose(self) -> bool:
        """Idempotent disposal: safe to call after a crash or double-close."""
        if self.disposed:
            return False
        self.disposed = True
        self.manager._drop(self)
        return True

    def __enter__(self) -> "GhidraSession":
        return self

    def __exit__(self, *exc) -> None:
        self.dispose()


class SessionManager:
    """Bounded, idly-evicted sessions with reliable disposal and explicit invalidation semantics.

    Long-lived live sessions are only worth their complexity once measurements justify them (P7.5: start with
    batched headless export and a persistent result cache); this manager bounds what a future live lane may
    hold (max_sessions, idle timeout) and owns cleanup so crashes never leave stale handles."""

    def __init__(self, cache_dir: Path | None = None, max_sessions: int = 4, idle_seconds: float = 1800.0):
        self.cache_dir = cache_dir
        self.max_sessions = max(1, max_sessions)
        self.idle_seconds = idle_seconds
        self._sessions: dict[str, GhidraSession] = {}
        self._lock = threading.RLock()
        self.invalidations: list[str] = []

    def acquire(self, key: str) -> GhidraSession:
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and not existing.disposed:
                existing.touch()
                return existing
            while len(self._sessions) >= self.max_sessions:
                idle = [s for s in self._sessions.values()
                        if time.monotonic() - s.last_used >= self.idle_seconds]
                victim = min(idle or self._sessions.values(), key=lambda s: s.last_used)
                victim.dispose()
            session = GhidraSession(key, self)
            self._sessions[key] = session
            return session

    def _drop(self, session: GhidraSession) -> None:
        with self._lock:
            if self._sessions.get(session.key) is session:
                del self._sessions[session.key]

    def invalidate_target(self, key: str) -> int:
        self.invalidations.append(key)
        return invalidate(self.cache_dir, key) if self.cache_dir is not None else 0

    def on_metadata_change(self, target: str, *, kind: str = "metadata", reason: str = "") -> int:
        """Invalidate cached evidence after a change. kind='metadata' (types/symbols/analysis options) maps to
        flushCache; kind='overlay' (the Program view is invalidated) additionally disposes the live session so
        the next acquisition resets/reimports."""
        if kind not in ("metadata", "overlay"):
            raise ValueError(f"unknown metadata-change kind {kind!r}: expected metadata or overlay")
        with self._lock:
            session = self._sessions.get(target)
            if kind == "overlay" and session is not None and not session.disposed:
                session.dispose()
        n = self.invalidate_target(target)
        self.invalidations.append(f"{target}:{kind}:{reason}")
        return n

    def evict_idle(self) -> int:
        now = time.monotonic()
        with self._lock:
            idle = [s for s in list(self._sessions.values()) if now - s.last_used >= self.idle_seconds]
            return sum(1 for s in idle if s.dispose())

    def dispose_all(self) -> int:
        with self._lock:
            return sum(1 for s in list(self._sessions.values()) if s.dispose())

    def sessions(self) -> dict[str, GhidraSession]:
        with self._lock:
            return dict(self._sessions)


# ---------------------------------------------------------------------------- proposals (P7.6)

@dataclass
class AnalysisProposal:
    """An analysis observation turned into a reviewable proposal. Evidence IDs and hypotheses are recorded;
    requested changes are minimal. Proposals are never applied directly: decomp.wtf's protected edit path
    (ForkEngine.edit_file/attempt) re-validates the current snapshot and matched set first."""

    id: str
    function_key: str
    provider: str
    evidence_ids: list = field(default_factory=list)
    hypotheses: list = field(default_factory=list)
    requested_changes: list = field(default_factory=list)
    confidence: float = 0.0
    blocked_reason: str = ""


def propose(dossier: FunctionDossier, *, max_proposals: int = 8) -> list[AnalysisProposal]:
    """High-value observations (P7.6): unresolved indirect calls, recovered types and thunk classifications."""
    out: list[AnalysisProposal] = []
    n = 0

    def add(**kw) -> None:
        nonlocal n
        if n < max_proposals:
            out.append(AnalysisProposal(
                function_key=dossier.key, provider="ghidra",
                blocked_reason="analysis proposals never mutate project state: apply through decomp.wtf's "
                               "protected edit path (ForkEngine.edit_file/attempt), which re-validates the "
                               "current snapshot", **kw))
            n += 1

    for i, call in enumerate(dossier.calls):
        if call.indirect and call.target is None:
            add(id=f"{dossier.key}:call:{i}",
                evidence_ids=[f"{dossier.key}:calls:{i}"],
                hypotheses=[f"the indirect call at {call.address:#x} has no resolved destination "
                            "(function pointer, vtable or switch-table dispatch)"],
                requested_changes=[{"kind": "observe", "target": "call", "address": call.address,
                                    "note": "no source change proposed; record for the call-graph"}],
                confidence=0.5)
    for i, t in enumerate(dossier.types):
        if t.origin == "decompiler" and not t.complete:
            add(id=f"{dossier.key}:type:{i}",
                evidence_ids=[f"{dossier.key}:types:{i}"],
                hypotheses=[f"recovered type {t.name!r} ({t.signature}) is decompiler-inferred, not original "
                            f"source truth: verify before adopting"],
                requested_changes=[{"kind": "type", "target": t.name, "before": None, "after": t.signature,
                                    "origin": t.origin, "complete": t.complete}],
                confidence=0.3)
    if dossier.classification == "thunk" and dossier.thunk_of is not None:
        add(id=f"{dossier.key}:thunk",
            evidence_ids=[f"{dossier.key}:classification"],
            hypotheses=[f"{dossier.name!r} is a thunk to {dossier.thunk_of:#x}: keep it classified as a thunk "
                        f"in the call graph rather than emitting a C body"],
            requested_changes=[{"kind": "classify", "target": dossier.name, "classification": "thunk",
                                "thunk_of": dossier.thunk_of}],
            confidence=0.9)
    return out


def apply_proposal(project_root: Path, proposal: AnalysisProposal) -> str:
    """Refuse direct mutation. There is deliberately no code path here that writes into `project_root`:
    analysis-proposed changes apply only through decomp.wtf's protected edit path (G-10)."""
    raise ProposalError(
        f"analysis proposal {proposal.id!r} for {proposal.function_key!r} cannot be applied directly. "
        f"decomp.wtf applies proposed changes only through its protected edit path "
        f"(ForkEngine.edit_file/attempt), which re-validates the current snapshot and matched set. "
        f"No project state was changed. Evidence: {', '.join(proposal.evidence_ids) or 'none'}")


# ---------------------------------------------------------------------------- alternative-draft portfolio (P7.7)

@dataclass
class DraftCandidate:
    provider: str
    kind: str  # best | m2c | ghidra | retrieved
    source: str
    provenance: dict = field(default_factory=dict)
    verified: bool | None = None


@dataclass
class DraftPortfolio:
    """Alternative drafts for one (usually stalled) function: the current best source, the matching-oriented
    m2c source, a Ghidra-assisted alternative and compatible retrieved source, each with provenance."""

    function_key: str
    candidates: list = field(default_factory=list)

    def by_kind(self, kind: str) -> DraftCandidate | None:
        return next((c for c in self.candidates if c.kind == kind), None)

    def best(self) -> DraftCandidate | None:
        return self.by_kind("best")

    def alternative(self) -> DraftCandidate | None:
        """The matching-oriented alternative. m2c remains the primary draft route; a Ghidra-assisted
        alternative is offered only when there is no verified near-match to demote. Ghidra C is never
        preferred merely because it is easier to read."""
        best = self.best()
        if best is not None and best.verified:
            return None
        m2c = self.by_kind("m2c")
        if m2c is not None:
            return m2c
        ghidra = self.by_kind("ghidra")
        if ghidra is not None and (best is None or not best.source.strip()):
            return ghidra
        return None


def make_portfolio(function_key: str, *, best: DraftCandidate | None = None,
                   m2c: DraftCandidate | None = None, ghidra: DraftCandidate | None = None,
                   retrieved: DraftCandidate | None = None) -> DraftPortfolio:
    return DraftPortfolio(function_key=function_key,
                          candidates=[c for c in (best, m2c, ghidra, retrieved) if c is not None])


# ------------------------------------------------------------------ ROM lane (PR-31)
# The ROM-evidence workflow proven on slus-01363: wrap the project's segment images into one
# little-endian MIPS ELF at their real vram, import headless with the pinned Ghidra, and decompile
# ONE target function with an explicit body range. Read-only evidence: nothing here mutates
# project state (analysis proposals still go through the protected edit path).

def _rom_elf_segments(cfg: dict, data: bytes, *, rom_offset: int | None = None) -> list[tuple[int, int, int, bytes]]:
    """(vaddr, filesz, memsz, payload) per config segment.

    Overlays share one vram range, so only one of them can be mapped. `rom_offset` (the function's
    offset in the ROM container) selects WHICH: the segment containing it wins the range, and the
    others are skipped. Without it the first segment of each range is kept, as before."""
    segs = cfg.get("segments", [])
    winner = None
    if rom_offset is not None:
        winner = next((s for s in segs if s["rom_start"] <= rom_offset < s["rom_end"]), None)
    out: list[tuple[int, int, int, bytes]] = []
    kept: list[tuple[int, int]] = []
    ordered = ([winner] if winner is not None else []) + [s for s in segs if s is not winner]
    for seg in ordered:
        start, end, vram = seg["rom_start"], seg["rom_end"], seg["vram"]
        rng = (vram, vram + end - start)
        # overlays are all mapped at one vram with different sizes: exact-key dedupe is not enough,
        # any vram overlap means the loader could only keep one of them (the last one wins, so a
        # stray overlay would silently replace the function's real bytes)
        if any(not (rng[1] <= k[0] or rng[0] >= k[1]) for k in kept):
            continue
        if end > len(data):
            continue
        kept.append(rng)
        out.append((vram, end - start, end - start, data[start:end]))
    return out


def rom_wrapper(proj: dict, out_dir: Path, *, rom_offset: int | None = None) -> Path:
    """One ELF32 (little-endian) wrapper of the project's segment images at their real vram
    addresses. `rom_offset` selects which overlay wins its vram range (see _rom_elf_segments).
    Cached by the project's rom identity plus the selected overlay; overwriting is safe."""
    import hashlib
    import struct

    cfg = json.loads(proj["config"])
    if cfg.get("big_endian"):
        raise AdmissionError("the ROM wrapper lane is little-endian only; big-endian ROMs are not admitted")
    rom = (Path(proj["root"]) / cfg["rom"]["file"]).read_bytes()
    segs = _rom_elf_segments(cfg, rom, rom_offset=rom_offset)
    digest = hashlib.sha256(rom + b"rom-wrapper-v3" + str(rom_offset or 0).encode()).hexdigest()[:16]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"rom-{proj['slug']}-{digest}.elf"
    if out.exists():
        return out
    off = 0x1000
    e_ident = b"\x7fELF\x01\x01\x01" + b"\x00" * 9
    hdr = e_ident + struct.pack("<HHIIIIIHHHHHH", 2, 8, 1, segs[0][0], 52, 0, 0, 52, 32, len(segs), 40, 0, 0)
    body = b""
    for vaddr, filesz, memsz, payload in segs:
        body += struct.pack("<IIIIIIII", 1, off, vaddr, vaddr, filesz, memsz, 5, 0x1000)
        off += filesz
    tmp = out.with_suffix(".tmp")
    pad = 0x1000 - len(hdr) - len(body)
    tmp.write_bytes(hdr + body + b"\x00" * pad + b"".join(s[3] for s in segs))
    tmp.rename(out)
    return out


def decompile_rom_function(proj: dict, addr: int, size: int, cache_dir: Path, *, timeout: int = 300,
                           rom_offset: int | None = None) -> dict:
    """Decompile one ROM function: wrap the project image, import headless with the pinned Ghidra
    (no auto-analysis; the shipped script disassembles the body, defines referenced data and
    decompiles), and return the evidence. Requires Ghidra + Java 21 (available())."""
    if not available():
        raise GhidraUnavailable("Ghidra (analyzeHeadless) and Java 21 are required for ROM evidence")
    scripts = Path(cache_dir) / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "DecompOne.java").write_text(
        importlib_resources().read_text("decompnet.forks.ghidra_scripts", "DecompOne.java"), encoding="utf-8")
    wrapper = rom_wrapper(proj, Path(cache_dir) / "rom", rom_offset=rom_offset)
    gh = ghidra_home()
    user_home = os.environ.get("DECOMP_GHIDRA_USER_HOME", str(Path(cache_dir) / "ghidra-user"))
    Path(user_home).mkdir(parents=True, exist_ok=True)
    proj_dir = Path(cache_dir) / "proj"
    proj_dir.mkdir(parents=True, exist_ok=True)
    argv = [str(gh / "support" / "analyzeHeadless"), str(proj_dir), f"rom{addr:x}",
            "-import", str(wrapper), "-processor", "MIPS:LE:32:default", "-noanalysis",
            "-postScript", "DecompOne.java", f"0x{addr:x}", str(size),
            "-scriptPath", str(scripts), "-deleteProject"]
    env = dict(os.environ, JAVA_HOME=str(java_home()), JAVA_TOOL_OPTIONS=f"-Duser.home={user_home}")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "c": None, "calls": [], "data_refs": [], "diagnostics": [f"exceeded {timeout}s"]}
    out = proc.stdout or ""
    if proc.returncode != 0:
        return {"status": "failed", "c": None, "calls": [], "data_refs": [],
                "diagnostics": (proc.stderr or "")[-800:].splitlines()}
    c = _between(out, "=== DECOMPILED C ===", "=== CALLS ===")
    calls = _lines_between(out, "=== CALLS ===", "=== DATA REFS")
    refs = _lines_after(out, "=== DATA REFS")
    status = "ok" if c and "halt_baddata" not in c else "partial"
    return {"status": status, "c": c, "calls": calls, "data_refs": refs,
            "diagnostics": ["halt_baddata: body contains undefined bytes — check the address/size"] if status == "partial" else []}


def _between(text: str, a: str, b: str) -> str | None:
    i = text.find(a)
    if i < 0:
        return None
    j = text.find(b, i)
    return text[i + len(a):j if j >= 0 else len(text)].strip()


def _lines_between(text: str, a: str, b: str) -> list[str]:
    seg = _between(text, a, b)
    return [l.strip() for l in (seg or "").splitlines() if l.strip()]


def _lines_after(text: str, marker: str) -> list[str]:
    i = text.find(marker)
    if i < 0:
        return []
    return [l.strip() for l in text[i + len(marker):].splitlines() if l.strip()]


def importlib_resources():
    from importlib import resources

    return resources
