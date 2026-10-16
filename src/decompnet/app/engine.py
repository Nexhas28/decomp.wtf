"""The local engine: ROM projects on top of the coordination core.

Network (claims, attempts, best attempts, notes, a regression-tested shared header) already does the
bookkeeping several agents need to work on one codebase. A ROM project adds:

- targets taken straight from the ROM (splat), matched by linking the candidate at the function's
  address (rom.match), so a match is byte-identical code;
- a symbol table, so agents can name things (rename_symbol) without breaking matches;
- tools for what ROMs need: split a function the disassembler merged, read data, see who calls what.

Everything stays on this computer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import struct
import threading
import time
from pathlib import Path

from .. import compiler_profiles as profiles
from .. import linkblock, retrieval, scoring, seeds, similarity, snapshots, telemetry, toolchains
from ..elfobj import FunctionObj
from ..network import (RELEASE_COOLDOWN, RESEARCH_PREFIX, SKIP_MAX, STRATEGIES, STRATEGY_INSNS_CAP, STRATEGY_WHERE, Agent,
                       DISCONNECTED, Network, NetworkError, _adjacent, _fragment_like, _hash_token, config_from, research_note_text)
from ..rom import detect, match, split
from ..rom.symbols import AUTO_NAME, IDENT, Symbol, SymbolTable

DEFAULT_TOOLCHAIN = {"KMC": "gcc-2.7.2-kmc", "IDO": "ido-7.1", "PSYQ": "gcc-2.7.2-psx", "EEGCC": "ee-gcc-2.96",
                     "AGBCC": "agbcc"}
# How ready each platform is for a full decomp you can port and mod; the GUI and the README show it.
# recommended: the most tested path, from import to a project that builds the game (GameCube and Wii: the same kind
# of project as decomp.dev's, linked file by file as it's finished; N64: tested on real games, rebuilt byte for byte).
# beta: works and is tested on homemade games, less so on real ones yet.
PLATFORM_STATUS = {"gc": "recommended", "wii": "recommended", "n64": "recommended",
                   "psx": "beta", "gba": "beta", "gb": "beta", "gbc": "beta", "nds": "beta"}  # PlayStation 2: switched off for now (rom/detect.py)
COMPILERS = {
    "gcc-2.7.2-kmc": "many third-party N64 games",
    "ido-7.1": "most later first-party N64 games",
    "ido-5.3": "early N64 games (1996-97)",
    "gcc-2.7.2-psx": "PlayStation games (PSY-Q era GCC)",
    "gcc-2.8.1-psx": "later PlayStation games",
    "gcc-2.5.7-psx": "the earliest PlayStation games",
    "gcc-2.6.0-psx": "early PlayStation games",
    "gcc-2.6.3-psx": "early PlayStation games",
    "gcc-2.7.2-cdk": "PlayStation games built with the CDK GCC 2.7.2",
    "gcc-2.8.0-psx": "later PlayStation games",
    "gcc-2.91.66-psx": "late PlayStation games (egcs)",
    "gcc-2.95.2-psx": "the last PlayStation games",
    "agbcc": "Game Boy Advance games (pret's agbcc, GCC 2.95)",
    "gbdk-2020": "Game Boy and Game Boy Color games (GBDK-2020 / SDCC)",
}
SHORT_NAMES = {"gcc-2.7.2-kmc": "KMC GCC 2.7.2", "ido-7.1": "IDO 7.1", "ido-5.3": "IDO 5.3",
               "gcc-2.7.2-psx": "GCC 2.7.2 (PS1)", "gcc-2.8.1-psx": "GCC 2.8.1 (PS1)",
               "gcc-2.5.7-psx": "GCC 2.5.7 (PS1)", "gcc-2.6.0-psx": "GCC 2.6.0 (PS1)", "gcc-2.6.3-psx": "GCC 2.6.3 (PS1)",
               "gcc-2.7.2-cdk": "GCC 2.7.2 CDK (PS1)", "gcc-2.8.0-psx": "GCC 2.8.0 (PS1)",
               "gcc-2.91.66-psx": "GCC 2.91.66 (PS1)", "gcc-2.95.2-psx": "GCC 2.95.2 (PS1)", "ee-gcc-2.96": "EE-GCC 2.96",
               "agbcc": "agbcc", "gbdk-2020": "GBDK-2020 (SDCC)", "mwcc-ps2": "MWCC PS2 3.0.1 b198"}
# CodeWarrior versions in the `mwcc` pack (GameCube, Wii) and the `mwccarm` pack (DS), for games started from scratch.
MW_VERSIONS = {
    "gc": ["GC/1.0", "GC/1.1", "GC/1.1p1", "GC/1.2.5", "GC/1.2.5n", "GC/1.3", "GC/1.3.2", "GC/1.3.2r", "GC/2.0",
           "GC/2.0p1", "GC/2.5", "GC/2.6", "GC/2.7", "GC/3.0a3", "GC/3.0a3.2", "GC/3.0a3.3", "GC/3.0a3.4", "GC/3.0a5",
           "GC/3.0a5.2"],
    "wii": ["Wii/0x4201_127", "Wii/1.0RC1", "Wii/1.0a", "Wii/1.0", "Wii/1.1", "Wii/1.3", "Wii/1.5", "Wii/1.6", "Wii/1.7"],
    "ds": ["DS/1.2/base", "DS/1.2/sp2", "DS/1.2/sp2p3", "DS/1.2/sp3", "DS/1.2/sp4", "DS/2.0/base", "DS/2.0/sp1",
           "DS/2.0/sp1p2", "DS/2.0/sp1p5", "DS/2.0/sp1p6", "DS/2.0/sp1p7", "DS/2.0/sp2", "DS/2.0/sp2p2", "DS/2.0/sp2p3",
           "DS/2.0/sp2p4", "DS/dsi/1.1", "DS/dsi/1.1p1", "DS/dsi/1.2", "DS/dsi/1.2p1", "DS/dsi/1.2p2", "DS/dsi/1.3",
           "DS/dsi/1.3p1", "DS/dsi/1.6sp1", "DS/dsi/1.6sp2"],
}

# Tools shown in Setup besides the compilers.
TOOLS = {
    "m2c": "machine-made first drafts (MIPS, PowerPC, ARM)",
    "objdiff-cli": "judges forks of public decomps, function by function",
    "ghidra": "drafts for PC, Switch and other platforms m2c can't read (540 MB, needs Java 21+)",
    "mwcc": "CodeWarrior, the compilers of GameCube and Wii games (proprietary)",
    "mwccarm": "CodeWarrior for DS, the compilers of DS games (proprietary)",
    "dsd": "ds-decomp: analyses DS ROMs and rebuilds them",
}
# Functions this small are handed out in batches (claim_function count): agents match most on the first try.
BATCH_INSNS = 48
PLATFORM_COMPILERS = {"n64": ["gcc-2.7.2-kmc", "ido-7.1", "ido-5.3"], "psx": ["gcc-2.7.2-psx", "gcc-2.8.1-psx",
                      "gcc-2.5.7-psx", "gcc-2.6.0-psx", "gcc-2.6.3-psx", "gcc-2.7.2-cdk", "gcc-2.8.0-psx",
                      "gcc-2.91.66-psx", "gcc-2.95.2-psx"],
                      "ps2": ["ee-gcc-2.96", "mwcc-ps2"], "gba": ["agbcc"],
                      "gb": ["gbdk-2020"], "gbc": ["gbdk-2020"]}
_AUTO_TOKEN = re.compile(r"\b(?:func|D|B|jtbl|RO|STR|FLT|DBL)_[0-9A-F]{8}\b")
# 0x-prefixed: any width (2-4 digits on Game Boy); bare: the full 8 digits only, so names that happen to be
# hex words ("add", "dead", "cafe") still resolve as names
_ADDRESS = re.compile(r"^(?:0x([0-9A-Fa-f]{1,8})|([0-9A-Fa-f]{8}))$", re.I)
ROOM_CLAIM_TRIES = 6  # host offers this copy can't take, given back before a room claim gives up
RESEARCH_HOLD = 30 * 60  # how long a function next_to_research handed one researcher is skipped for the others
_HOST_HOLDS = re.compile(r"^you already hold (\S+?);")


def _host_refused(score) -> str:
    return (f"❌ NOT A MATCH YET: your copy scored 0, but the room's host scored "
            f"{score if score is not None else 'it as not compiling'}. The host's judge is the one that counts (its "
            "compiler settings, its decomp-* comment profiles, its cut of the function): keep iterating. Your claim "
            "is kept.")


def _host_cut_differs(fn, host_size: int) -> str:
    return (f"❌ NOT A MATCH HERE: the room's host matched its {fn['name']}, but the host's function is {host_size} "
            f"bytes and this copy's is {fn['size']} (the two copies cut this part of the game differently, e.g. the "
            "host merged a false split). This copy's function is other code, so the match can't be kept here. "
            "Ask the host to share its cut, or work on another function.")


def _address(text: str) -> int | None:
    m = _ADDRESS.match(text.strip()) if text else None
    return int(m.group(1) or m.group(2), 16) if m else None

GP_NOTE = """
/* This game keeps small globals (8 bytes or less) in $gp-relative small data. Declare those here WITHOUT
 * `extern` (for example `s32 D_8001B278;`): then the compiler reads them through $gp, like the game does.
 * With `extern`, it builds the full address instead and the function can't match. */
"""

BASE_HEADER = """/* context.h for {title}: shared by every function. Agents grow it with edit_context:
 * add a prototype or struct the moment you work one out, so the next function benefits. */

typedef signed char s8;
typedef unsigned char u8;
typedef signed short s16;
typedef unsigned short u16;
typedef signed int s32;
typedef unsigned int u32;
typedef signed long long s64;
typedef unsigned long long u64;
typedef float f32;
typedef double f64;
typedef volatile u8 vu8;
typedef volatile u16 vu16;
typedef volatile u32 vu32;
typedef volatile s32 vs32;
typedef int s32_bool;

#define NULL ((void *)0)
#define TRUE 1
#define FALSE 0
#define ARRAY_COUNT(arr) (s32)(sizeof(arr) / sizeof(arr[0]))
"""


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:40] or "rom"


def _preview(data: bytes | None, kind: str) -> str:
    if not data:
        return ""
    text = data.split(b"\0", 1)[0]
    if len(text) >= 3 and all(32 <= c < 127 or c in (9, 10) for c in text):
        return json.dumps(text.decode("ascii"))
    return data[:16].hex(" ")


def _app_home_for(db_path: Path | str | None) -> Path | None:
    """The app home whose own database this is, or None when it is not an app instance's database.

    The lockdown is an operator setting for ONE instance. Scoping it to the database it protects is what
    keeps it from leaking into every other RomNetwork in the process — tests, a scratch copy, or a second
    instance with a different home — which it must never do (it refuses to create agent identities)."""
    if db_path is None:
        return None
    home = Path(os.environ.get("DECOMP_HOME", str(Path.home() / "Decomp"))).expanduser()
    try:
        if Path(db_path).resolve() == (home / ".decomp" / "decomp.db").resolve():
            return home
    except OSError:
        return None
    return None


def _lockdown_settings(db_path: Path | str | None = None) -> dict:
    """Operator fleet lockdown (added after a runaway agent swarm): a small JSON file in the app home

        <DECOMP_HOME>/.decomp/settings.json   {"lockdown": true, "allowed_agents": ["name", ...]}

    While `lockdown` is true, the app refuses to CREATE new agent identities — an existing identity keeps
    working (tokens still rotate), and names in `allowed_agents` are always allowed. Nothing else about
    the app changes. It applies ONLY to the instance that owns `<home>/.decomp/decomp.db`: other networks
    in the same process (tests, scratch copies, another home) are untouched.
    `DECOMP_LOCKDOWN=1` forces it on and `DECOMP_LOCKDOWN=0` forces it off, which is what the tests use.
    Unreadable or malformed settings mean "no lockdown": a broken file must never lock the app out."""
    forced = os.environ.get("DECOMP_LOCKDOWN")
    home = _app_home_for(db_path)
    if home is None and forced is None:
        return {}  # not this instance's setting: never read (and never inherit) another instance's policy
    data: dict = {}
    if home is not None:
        try:
            loaded = json.loads((home / ".decomp" / "settings.json").read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    if forced is not None:
        data["lockdown"] = forced.strip().lower() not in ("", "0", "false", "no", "off")
    return data


def _lockdown_refuses(name: str, db_path: Path | str | None = None) -> str | None:
    """The refusal message when the lockdown forbids creating `name` as a NEW identity, else None."""
    settings = _lockdown_settings(db_path)
    if not settings.get("lockdown"):
        return None
    allowed = {str(a).strip().lower() for a in (settings.get("allowed_agents") or [])}
    if name.strip().lower() in allowed:
        return None
    return (f"refusing to create a new agent identity {name!r}: this machine is in fleet lockdown "
            f"(a runaway swarm was spawning worker agents). Existing identities still work. To allow it, "
            f"add the name to allowed_agents in <DECOMP_HOME>/.decomp/settings.json or set lockdown to false.")


def _file_sha256(path: Path) -> str | None:
    """sha256 of an imported ROM file (its Section 18.2 artifact digest), or None when it is not on disk."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class RomNetwork(Network):
    def __init__(self, db_path: str | Path, workspace: Path, claim_ttl: int = 30 * 60):
        super().__init__(db_path, claim_ttl=claim_ttl)
        self.db_path = Path(db_path)
        self.workspace = Path(workspace)
        self._rebase_roots()
        self._syms: dict[int, SymbolTable] = {}
        self._sym_override: dict[int, SymbolTable] = {}
        self._roms: dict[int, tuple[bytes, list[split.Segment]]] = {}
        self._xrefs: dict[int, dict] = {}
        self._seed_lock = threading.Lock()
        self.links: dict = {}  # slug -> collab.Link, for projects joined from a friend's room
        self.rooms: dict = {}  # slug -> collab.Room, for projects this computer shares
        self._remote_ids: dict[str, int] = {}
        self._ops: dict[int, dict] = {}  # project id -> look-alike index
        self._diffs: dict[tuple, dict] = {}  # (function, source, context, compiler) -> diff for the GUI
        self.autopilots: dict[str, dict] = {}  # slug -> progress of trying every function's machine draft
        # Retrieval indexes (plan P10.2/P10.3): None = not loaded yet; a later loader/PR injects the
        # corpus-built Function ID and BSim indexes. Retrieval stays off until then (and by default).
        self.retrieval_fid: retrieval.FunctionIdIndex | None = None
        self.retrieval_bsim: retrieval.BsimIndex | None = None
        self.autopilot_after_match = False
        self._autopilot_restart_lock = threading.Lock()
        from .forkengine import ForkEngine  # forks build on this class's claims, attempts and live view
        from .permute import Permuter
        from .twins import Twins
        self.forks = ForkEngine(self)
        self.twins = Twins(self)
        self.permuter = Permuter(self)
        self._rescanning: set[int] = set()
        self._set_aside_checked: set[int] = set()  # projects whose BIOS calls and assembly were set aside on opening
        self._active_attempts: dict[int, int] = {}
        self._rescan_previews: dict[str, tuple[str, list, dict]] = {}
        self.rescan_busy = lambda slug: False  # server supplies its built-in agent/session guard
        self.starred_only: dict[int, float] = {}  # helper agent id -> when its session began (★ Next in line only)
        self.focus: dict[int, str] = {}  # helper agent id -> the strategy its Focus (Quick Wins, Close Calls) claims by
        self.fixer_ids: set[int] = set()  # agent ids of auto-fixer sessions: the only ones its tools work for
        # set by the server: a note was left on this copy's own project (slug, function name, text, agent)
        self.on_note = lambda slug, function, text, agent: None
        self.role: dict[int, str] = {}  # helper agent id -> its job when it isn't matching ("researcher")
        self.role_model: dict[int, str] = {}  # researcher agent id -> the model it runs (for what it adds to .okf)
        # function id -> (researcher agent id, until): what next_to_research handed out, so two researchers don't
        # write notes on the same function. Never a claim: matchers ignore it.
        self._research_held: dict[int, tuple[int, float]] = {}
        self._research_lock = threading.Lock()
        # researcher agent id -> the one function a person asked it to research (the function page's Research button)
        self.research_pin: dict[int, str] = {}

    # ------------------------------------------------------------------ basics

    def _rebase_roots(self) -> None:
        """A workspace can be opened from another place than the one that made it (another Mac, or a folder that was
        moved or renamed): a project whose stored folder is gone, but whose slug folder sits in this workspace,
        moves there. Opening it where it was made moves it back the same way."""
        for p in self.store.all("SELECT id, slug, root FROM projects"):
            here = self.workspace / p["slug"]
            if p["root"] and not Path(p["root"]).exists() and here.is_dir():
                with self.store.tx() as db:
                    db.execute("UPDATE projects SET root=? WHERE id=?", (str(here), p["id"]))

    def _require_budget(self, agent: Agent) -> None:
        """Your machine, your agent: no compile budget."""

    @staticmethod
    def cfg(proj) -> dict:
        return json.loads(proj["config"])

    def is_rom(self, proj) -> bool:
        return self.cfg(proj).get("kind") == "rom"

    def project_row(self, slug: str):
        return self._project(slug)

    def fork_slug(self, project: str | None) -> str | None:
        """The slug when `project` is a fork of a public decomp, else None."""
        try:
            proj = self._project(project)
        except NetworkError:
            return None
        return proj["slug"] if self.cfg(proj).get("kind") == "fork" else None

    def _rom_only(self, project: str | None, what: str) -> None:
        if self.fork_slug(project):
            raise NetworkError(f"{what} is for ROM projects. In a fork, the project's own headers and source files are "
                               "the shared context: use read_file, search_code and edit_file")

    def symbols(self, project_id: int) -> SymbolTable:
        if project_id in self._sym_override:
            return self._sym_override[project_id]
        if project_id not in self._syms:
            rows = self.store.all("SELECT name, address, size, kind FROM symbols WHERE project_id=?", project_id)
            self._syms[project_id] = SymbolTable([Symbol(r["name"], r["address"], r["size"], r["kind"]) for r in rows])
        return self._syms[project_id]

    def _rom(self, proj) -> tuple[bytes, list[split.Segment]]:
        if proj["id"] not in self._roms:
            cfg = self.cfg(proj)
            rom = (Path(proj["root"]) / cfg["rom"]["file"]).read_bytes()
            segs = [split.Segment(**s) for s in cfg["segments"]]
            self._roms[proj["id"]] = (rom, segs)
        return self._roms[proj["id"]]

    def reader(self, proj, fn=None):
        """Reads ROM bytes by load address. Overlays share load addresses, so with `fn` the segment holding that
        function's code is tried first: its jump tables and rodata come from its own overlay, not another one."""
        rom, segs = self._rom(proj)
        home = self._home_segment(rom, segs, fn) if fn is not None else None
        order = [home] + [s for s in segs if s is not home] if home is not None else segs

        def read(addr: int, n: int) -> bytes | None:
            for seg in order:
                if seg.contains(addr, n):
                    at = seg.rom_start + addr - seg.vram
                    return rom[at:at + n]
            return None
        return read

    @staticmethod
    def _home_segment(rom: bytes, segs: list[split.Segment], fn) -> split.Segment | None:
        """The segment whose bytes at the function's address are the function's own code."""
        try:
            vram, code = fn["vram"], fn["target"]
        except (KeyError, IndexError):
            return None
        if vram is None or not code:
            return None
        hits = [s for s in segs if s.contains(vram, len(code))]
        if len(hits) < 2:
            return hits[0] if hits else None
        for seg in hits:
            at = seg.rom_start + vram - seg.vram
            if rom[at:at + len(code)] == code:
                return seg
        return hits[0]

    def _function(self, name: str, project: str | None):
        """Functions can also be named by address (0x80001234), handy for agents reading assembly, and the way
        agents write names they read elsewhere (see _loose_functions)."""
        addr = _address(name)
        if addr is not None:
            proj = self._project(project)
            rows = self.store.all("SELECT * FROM functions WHERE project_id=? AND vram=?", proj["id"], addr)
            if len(rows) == 1:
                return rows[0]
            if rows:  # Game Boy banks share CPU addresses: the address alone doesn't say which one
                raise NetworkError(f"0x{addr:X} is in more than one bank: name the function instead "
                                   f"({', '.join(r['name'] for r in rows[:8])})")
        try:
            return super()._function(name, project)
        except NetworkError as e:
            rows = self._loose_functions(name, project) if name else []
            if len(rows) == 1:
                return rows[0]
            if rows:
                raise NetworkError(self._ambiguous(name, rows)) from None
            near = self._near_names(name, project) if name else []
            if near:
                raise NetworkError(f"{e}. Did you mean: {', '.join(near)}?") from None
            raise

    def _near_names(self, name: str, project: str | None, n: int = 5) -> list[str]:
        """Function names close to one that doesn't exist (a typo, another spelling of the symbol)."""
        import difflib

        try:
            proj = self._project(project)
        except NetworkError:
            return []
        sym = name.strip().strip("`'\"").split("@", 1)[0].split("(", 1)[0].strip()
        if len(sym) < 3:
            return []
        rows = self.store.all("SELECT name FROM functions WHERE project_id=? AND (name LIKE ? OR name LIKE ?) LIMIT 4000",
                              proj["id"], f"%{sym[-6:]}%", f"{sym[:4]}%")
        names = [r["name"] for r in rows]
        return difflib.get_close_matches(sym, names, n=n, cutoff=0.6) or [x for x in names if sym in x][:n]

    def _loose_functions(self, name: str, project: str | None) -> list:
        """Functions an agent may mean by a name that isn't stored as such: the plain symbol of a function whose
        name repeats across a fork's files (stored as symbol@unit), symbol@unit for one that doesn't repeat,
        Class::method, a name with its parameter list or in quotes."""
        try:
            proj = self._project(project)
        except NetworkError:
            return []
        text = name.strip().strip("`'\"").strip()
        sym, _, unit = text.partition("@")
        unit, _, nth = unit.partition("#")
        sym = sym.split("(", 1)[0].strip()
        if not sym:
            return []
        rows = self.store.all(
            "SELECT * FROM functions WHERE project_id=? AND (name=? OR substr(name,1,?)=? "
            "OR json_extract(meta,'$.symbol')=? OR substr(json_extract(meta,'$.demangled'),1,?)=?) ORDER BY name",
            proj["id"], sym, len(sym) + 1, sym + "@", sym, len(sym) + 1, sym + "(")
        if unit:
            rows = ([r for r in rows if r["unit"] == unit] or [r for r in rows if r["unit"].endswith(unit)]
                    or [r for r in rows if r["unit"].startswith(unit)] or [r for r in rows if unit in r["unit"]])
        if nth.isdigit() and len(rows) > 1:
            rows = [r for r in rows if (json.loads(r["meta"] or "{}").get("nth", 0) + 1) == int(nth)] or rows
        return [r for r in rows if r["status"] != "nosource"] or rows  # ones in files without source can't be worked on

    @staticmethod
    def _ambiguous(name: str, rows: list) -> str:
        listing = "\n".join(f"- {r['name']} ({r['status']}, {r['n_insns']} instructions)" for r in rows[:12])
        more = f"\n... and {len(rows) - 12} more" if len(rows) > 12 else ""
        return (f"{name!r} is the name of {len(rows)} functions in different files; pass the full name:\n"
                f"{listing}{more}")

    def resolve_function(self, agent: Agent | None, project: str | None, name: str) -> str:
        """The stored name for what an agent calls a function: when a plain name fits several, the one the agent
        holds a claim on."""
        try:
            return self._function(name, project)["name"]
        except NetworkError:
            rows = self._loose_functions(name, project) if name else []
            held = self.store.all("SELECT f.id, f.unit FROM claims c JOIN functions f ON f.id=c.function_id "
                                  "WHERE c.agent_id=?", agent.id) if agent else []
            if held and len(rows) > 1:  # one it holds, else the one in the file (or module) it works in
                ids, units = {h["id"] for h in held}, {h["unit"] for h in held}
                modules = {u.split("/", 1)[0] for u in units}
                for pick in ([r for r in rows if r["id"] in ids], [r for r in rows if r["unit"] in units],
                             [r for r in rows if r["unit"].split("/", 1)[0] in modules]):
                    if len(pick) == 1:
                        return pick[0]["name"]
            raise

    def _rom_target(self, fn) -> match.RomTarget:
        proj = self.store.one("SELECT config FROM projects WHERE id=?", fn["project_id"])
        cfg = json.loads(proj["config"])
        return match.RomTarget(fn["name"], fn["vram"], fn["target"], match.RomTarget.relocs_from_json(json.loads(fn["relocs"])),
                               cfg.get("big_endian", True), cfg.get("gp", 0))

    def _make_target(self, proj, rf: split.RomFunction, syms: SymbolTable) -> match.RomTarget:
        cfg = self.cfg(proj)
        return match.target_from_split(rf, syms.resolve, cfg.get("big_endian", True), cfg.get("gp", 0))

    def _target_obj(self, fn, proj):
        if not self.is_rom(proj):
            return super()._target_obj(fn, proj)
        if (self.cfg(proj).get("arch") or "").lower() == "sm83":
            return FunctionObj(fn["name"], "sm83", False, fn["target"], [])
        if self.cfg(proj).get("platform") == "gba":
            return FunctionObj(fn["name"], "thumb", False, fn["target"], [])
        return match.canonical_target(self._rom_target(fn), self.symbols(proj["id"]))

    def _evaluate(self, fn, proj, obj: bytes):
        if not self.is_rom(proj):
            return super()._evaluate(fn, proj, obj)
        if (self.cfg(proj).get("arch") or "").lower() == "sm83":
            # The Game Boy lane compiles in absolute mode (the ROM's own symbol addresses are injected),
            # so the candidate's bytes can be compared with the target's directly — no relocations to mask.
            from ..elfobj import function_from_payload

            cand = function_from_payload(obj, fn["name"])
            if cand is None:
                return None, None, [f"the candidate didn't define {fn['name']}"]
            exact = cand.code == fn["target"]
            return cand, exact, ([] if exact else ["the compiled bytes differ from the ROM's"])
        if self.cfg(proj).get("platform") == "gba":
            from ..rom import match_arm

            res = match_arm.link(obj, fn["name"], match_arm.ArmTarget(fn["name"], fn["vram"], fn["target"]),
                                 self.symbols(proj["id"]), self.reader(proj))
            return res.candidate, res.exact if res.candidate is not None else None, res.problems
        res = match.link(obj, fn["name"], self._rom_target(fn), self.symbols(proj["id"]), self.reader(proj, fn))
        return res.candidate, res.exact if res.candidate is not None else None, res.problems

    def _twin_lines(self, proj, fn) -> list[str]:
        """For a brief: the matched function with the same code, and its C with this one's names."""
        try:
            twin_id = self.twins.twin_of(proj, fn["id"])
        except Exception:
            return []
        if twin_id is None:
            return []
        a = self.store.one("SELECT * FROM functions WHERE id=?", twin_id)
        source = self.twins.source_for(proj, a, fn)
        out = ["", f"### Identical code: {a['name']} is the same machine code and already matches"]
        if source and len(source) < 6000:
            out += ["Its C with this function's names (try it first):", "```c", source.strip(), "```"]
        return out

    def rename_tokens(self, proj, text: str) -> str:
        """Show automatic names under their current names (func_80001234 -> deflate)."""
        syms = self.symbols(proj["id"])
        return _AUTO_TOKEN.sub(lambda m: syms.label(int(m.group(0)[-8:], 16)), text)

    def _asm_text(self, fn, proj) -> str:
        if not self.is_rom(proj):
            return super()._asm_text(fn, proj)
        return self.rename_tokens(proj, fn["asm"])

    # ------------------------------------------------------------------ import

    def import_rom(self, data: bytes | Path, *, toolchain: str | None = None, flags: list[str] | None = None,
                   confirmed: bool = False, name: str = "", log=print) -> str:
        """Import a game the user owns (ROM bytes, or a file such as a disc image): split it, store every function
        as a target. Returns the slug."""
        if not confirmed:
            raise NetworkError("confirm that this is your own legally obtained copy of the game")
        project_file = self._community_layout(data, name)
        if project_file is not None:  # a dtk-template or ds-decomp project, worked on like a fork
            compiler = toolchain if toolchain and toolchain.startswith(("GC/", "Wii/", "DS/")) else None
            return self.forks.create_scratch(project_file, compiler=compiler, log=log)
        picked = detect.identify(data, name)
        rom, info = picked.data, picked.info
        for p in self.store.all("SELECT slug, config FROM projects"):
            if json.loads(p["config"]).get("rom", {}).get("sha1") == info["sha1"]:
                log(f"already imported as {p['slug']}")
                return p["slug"]
        base = _slugify(info["title"])
        slug, n = base, 2
        while self.store.one("SELECT 1 FROM projects WHERE slug=?", slug) or (self.workspace / slug).exists():
            slug, n = f"{base}-{n}", n + 1
        workdir = self.workspace / slug
        workdir.mkdir(parents=True)
        (workdir / picked.filename).write_bytes(rom)
        size = f"{info['size'] // (1024 * 1024)} MiB" if info["size"] >= 1 << 20 else f"{info['size'] // 1024} KiB"
        log(f"{detect.PLATFORMS[picked.platform]}: {info['title']} ({info['game_code'] or 'no game code'}, "
            f"{info['region']}), {size}, looks built with {info['compiler_guess']}")
        if picked.platform == "gba":
            return self._import_gba(picked, slug, workdir, toolchain, flags, log)
        if picked.platform in ("gb", "gbc"):
            return self._import_gb(picked, slug, workdir, log)
        if picked.overlays:
            log(f"{len(picked.overlays)} overlay{'s' if len(picked.overlays) != 1 else ''} on the disc: "
                + ", ".join(f"{o[0]} at 0x{o[2]:08X}" for o in picked.overlays[:6]) + (", ..." if len(picked.overlays) > 6 else ""))
        res = split.split_rom(workdir, picked.filename, log=log, overlays=picked.overlays)
        if res.image and res.image != picked.filename:  # splat works on a flat image made from the program
            rom = (workdir / res.image).read_bytes()

        toolchain = toolchain or DEFAULT_TOOLCHAIN.get(res.compiler, DEFAULT_TOOLCHAIN[info["compiler_guess"]])
        tc = toolchains.get(toolchain)
        default_flags = tc.flags
        if picked.platform in ("psx", "ps2"):
            default_flags = [f for f in tc.flags if not f.startswith("-G")] + [f"-G{8 if res.gp else 0}"]
        cfg = {
            "kind": "rom", "platform": picked.platform, "big_endian": picked.big_endian, "gp": res.gp,
            "rom": {**info, "file": res.image or picked.filename, "program": picked.filename},
            "segments": [vars(s) for s in res.segments],
            # the import picked a toolchain from guesses/defaults: a hypothesis, recorded as such
            "compiler": toolchains.profile_from({"toolchain": toolchain, "flags": flags or default_flags,
                                                 "timeout": 20, "wrapper": "cmd" if tc.cmd else "cc",
                                                 "provenance": toolchains.PROVENANCE_GUESS}).to_dict(),
            "legal": {"basis": "personal-copy", "license": "personal use only",
                      "statement": "imported by the owner of this copy of the game, who confirmed it was legally obtained",
                      "confirmed_at": time.time()},
            "context": {"file": None},
        }
        symbols = SymbolTable()
        for name, addr in res.named.items():
            symbols.add(Symbol(name, addr, 0, "func"), reindex=False)
        for fn in res.functions:
            symbols.add(Symbol(fn.name, fn.vram, fn.size, "func"), reindex=False)
        rom_bytes, segs = rom, res.segments

        def read(addr, n):
            for seg in segs:
                if seg.contains(addr, n):
                    at = seg.rom_start + addr - seg.vram
                    return rom_bytes[at:at + n]
            return None

        data_rows = []
        for d in res.data:
            if d.name in symbols.by_name:
                continue
            symbols.add(Symbol(d.name, d.vram, d.size, d.kind), reindex=False)
            data_rows.append((d.name, d.vram, d.size, d.kind, _preview(read(d.vram, min(d.size, 48)), d.kind) if d.kind != "bss" else ""))
        symbols._reindex()

        now = time.time()
        with self.store.tx() as db:
            pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                             (slug, info["title"], f"{info['game_code']} · {info['region']} · {len(res.functions)} functions",
                              "mips", int(picked.big_endian), str(workdir), json.dumps(cfg))).lastrowid
            for fn in res.functions:
                target = match.target_from_split(fn, symbols.resolve, picked.big_endian, res.gp)
                db.execute(
                    "INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, fn.name, fn.size, len(fn.lines), target.code, json.dumps(target.to_json()),
                     self._asm_source(fn), None, fn.segment, fn.vram, fn.rom))
            for s in symbols.by_name.values():
                preview = next((r[4] for r in data_rows if r[0] == s.name), None)
                db.execute("INSERT INTO symbols(project_id,name,address,size,kind,preview) VALUES(?,?,?,?,?,?)",
                           (pid, s.name, s.address, s.size, s.kind, preview))
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (pid, 1, BASE_HEADER.format(title=info["title"]) + (GP_NOTE if res.gp else ""),
                                                  None, "basic types", now, ""))
            self.store.record_rom_identities(db, pid, hashlib.sha256(picked.data).hexdigest())
            self._event(db, pid, None, "import", f"{len(res.functions)} functions from {info['title']}")
        log(f"ready: {len(res.functions)} functions to decompile")
        return slug

    def _import_gba(self, picked, slug: str, workdir: Path, toolchain: str | None, flags: list[str] | None, log) -> str:
        """Game Boy Advance: functions found by luvdis (plus a pass for code nothing calls), judged by the ARM matcher."""
        from ..rom import gba

        info = picked.info
        fns = gba.split(workdir / picked.filename, workdir, log=log)
        toolchain = toolchain or "agbcc"
        tc = toolchains.get(toolchain)
        cfg = {
            "kind": "rom", "platform": "gba", "big_endian": False, "gp": 0, "arch": "thumb",
            "rom": {**info, "file": picked.filename, "program": picked.filename},
            "segments": [{"name": "rom", "rom_start": 0, "rom_end": info["size"], "vram": gba.BASE}],
            # the import picked a toolchain from guesses/defaults: a hypothesis, recorded as such
            "compiler": toolchains.profile_from({"toolchain": toolchain, "flags": flags or tc.flags,
                                                 "timeout": 20, "wrapper": "cmd" if tc.cmd else "cc",
                                                 "provenance": toolchains.PROVENANCE_GUESS}).to_dict(),
            "legal": {"basis": "personal-copy", "license": "personal use only",
                      "statement": "imported by the owner of this copy of the game, who confirmed it was legally obtained",
                      "confirmed_at": time.time()},
            "context": {"file": None},
        }
        now = time.time()
        with self.store.tx() as db:
            pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                             (slug, info["title"], f"{info['game_code']} · {info['region']} · {len(fns)} functions",
                              "arm", 0, str(workdir), json.dumps(cfg))).lastrowid
            names: set[str] = set()
            for f in fns:
                name = f.asm_only or f.name  # known hand-written helpers keep their real name and stay assembly
                if name in names:  # a game can link a helper twice (Spyro Orange has two of _call_via_r3)
                    name = f"{name}_{f.vram:08X}"
                names.add(name)
                db.execute(
                    "INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset,status) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, name, len(f.code), f.n_insns, f.code, json.dumps([list(r) for r in f.refs or []]),
                     f.asm.replace(f.name, name), None, "rom", f.vram, f.rom, "asm" if f.asm_only else "open"))
                db.execute("INSERT INTO symbols(project_id,name,address,size,kind,preview) VALUES(?,?,?,?,?,?)",
                           (pid, name, f.vram, len(f.code), "func", None))
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (pid, 1, BASE_HEADER.format(title=info["title"]), None, "basic types", now, ""))
            self.store.record_rom_identities(db, pid, _file_sha256(workdir / picked.filename))
            self._event(db, pid, None, "import", f"{len(fns)} functions from {info['title']}")
        log(f"ready: {len(fns)} functions to decompile")
        return slug

    def _import_gb(self, picked, slug: str, workdir: Path, log) -> str:
        """Game Boy / Game Boy Color: functions found by the fork's own SM83 disassembler.

        The reading lane is complete (detection, header, disassembly, function discovery), but the
        *matching* lane is not: byte-identical Game Boy C needs an SDCC/GBDK toolchain this app does not
        ship yet. Functions are therefore imported as `asm` — visible, searchable, disassembled and
        briefable, and deliberately NOT claimable, so no agent burns compiles on a lane that cannot
        judge a candidate yet. When the toolchain lands they become `open` (see benchmarks/ROADMAP.md)."""
        from ..rom import gb

        from ..rom import gb_build

        info = picked.info
        fns = gb.split(workdir / picked.filename, workdir, log=log)
        # The matching lane is live when the pinned GBDK-2020 toolchain is installed: then functions are
        # claimable (agents compile through rom/gb_build) and the profile names it. Without it the project
        # stays readable-only, which is the honest state rather than handing out work nobody can judge.
        matchable = gb_build.available()
        compiler = ({"toolchain": gb_build.TOOLCHAIN, "flags": ["--opt-code-speed"], "timeout": 120,
                     "wrapper": "cc", "provenance": "explicit",
                     "note": "SDCC/GBDK-2020 through decompnet.rom.gb_build (absolute symbol mode)"}
                    if matchable else
                    {"toolchain": "", "flags": [], "timeout": 20, "wrapper": "cc",
                     "provenance": "unavailable",
                     "note": "the Game Boy C toolchain (GBDK-2020) is not installed: run `decomp setup`"})
        # One segment per 16 KiB bank: bank 0 occupies 0x0000-0x3FFF, every other bank is switched into
        # the 0x4000-0x7FFF window (the same window for all of them, which is what the hardware does).
        segments = [{"name": f"bank{b:03d}", "rom_start": b * gb.BANK_SIZE,
                     "rom_end": min((b + 1) * gb.BANK_SIZE, info["size"]),
                     "vram": 0 if b == 0 else 0x4000}
                    for b in range(max(1, info["size"] // gb.BANK_SIZE))]
        cfg = {
            "kind": "rom", "platform": info["platform"], "big_endian": False, "gp": 0, "arch": "sm83",
            "rom": {**info, "file": picked.filename, "program": picked.filename},
            "segments": segments,
            "compiler": compiler,
            "legal": {"basis": "personal-copy", "license": "personal use only",
                      "statement": "imported by the owner of this copy of the game, who confirmed it was legally obtained",
                      "confirmed_at": time.time()},
            "context": {"file": None},
        }
        now = time.time()
        with self.store.tx() as db:
            pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                             (slug, info["title"],
                              f"{info['cartridge']} · {info['cgb']['mode']} · {len(fns)} functions · "
                              + ("matched with SDCC/GBDK-2020" if matchable else "reading lane only (no C toolchain yet)"),
                              "sm83", 0, str(workdir), json.dumps(cfg))).lastrowid
            names: set[str] = set()
            for f in fns:
                name = f.name
                if name in names:  # two banks can share a CPU address; the bank in the name prevents it
                    name = f"{name}_{f.rom:06x}"
                names.add(name)
                db.execute(
                    "INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset,status) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    # vram is the CPU address the ROM's instructions use; rom_offset the flat file offset;
                    # the unit is the bank, so briefs and context edits read per bank.
                    (pid, name, f.size, f.n_insns, f.code, json.dumps([list(r) for r in f.refs or []]),
                     f.asm, None, f"bank{f.bank:03d}", f.vram, f.rom, "open" if matchable else "asm"))
                db.execute("INSERT INTO symbols(project_id,name,address,size,kind,preview) VALUES(?,?,?,?,?,?)",
                           (pid, name, f.vram, f.size, "func", None))
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (pid, 1, BASE_HEADER.format(title=info["title"]), None, "basic types", now, ""))
            self.store.record_rom_identities(db, pid, _file_sha256(workdir / picked.filename))
            self._event(db, pid, None, "import", f"{len(fns)} functions from {info['title']}")
        if matchable:
            log(f"ready: {len(fns)} functions to decompile with SDCC/GBDK-2020 ({gb_build.TOOLCHAIN})")
        else:
            log(f"ready: {len(fns)} functions disassembled. The C toolchain (GBDK-2020) is not installed, "
                f"so they are shown as assembly until it is")
        return slug

    def _compile(self, proj, header: str, source: str, fn=None):
        """Game Boy candidates go through gb_build (SDCC + sdasgb) instead of the ELF pipeline: the
        project's own symbol table is injected, so the candidate's call/jump addresses are the ROM's and
        `_evaluate` can compare bytes directly. The payload carries every function the source defines;
        `_evaluate` picks the one being judged by name."""
        if self.is_rom(proj) and (self.cfg(proj).get("arch") or "").lower() == "sm83":
            from ..compiler import CompileResult
            from ..rom import gb_build

            symbols = {n: sym.address for n, sym in self.symbols(proj["id"]).by_name.items()}
            cfg = self.cfg(proj)
            inc = tuple(str((Path(proj["root"]) / p).resolve()) for p in (cfg.get("compiler") or {}).get("include_dirs", []))
            try:
                payload = gb_build.build_payload(source, context_header=header, symbols=symbols,
                                                 include_dirs=inc)
            except Exception as e:  # a build failure is a compile failure, reported like any other
                return CompileResult(False, None, f"{type(e).__name__}: {e}")
            return CompileResult(True, payload, "")
        return super()._compile(proj, header, source, fn)

    def rescan_gba(self, slug: str, *, apply: bool = False, revision: str | None = None, log=print) -> dict:
        """Preview edited functions.cfg; apply only against the exact preview revision.

        Affected targets and their full history are archived atomically; unchanged
        targets keep their IDs, matches, attempts and notes. No worker is started.
        """
        from .rescan import rescan_gba

        return rescan_gba(self, slug, apply=apply, revision=revision, log=log)

    def _require_not_rescanning(self, pid: int) -> None:
        if pid in self._rescanning:
            raise NetworkError("a GBA rescan is in progress; wait for it to finish before starting work")

    def _community_layout(self, data: bytes | Path, name: str = "") -> Path | None:
        """The file, when the game starts as a community-layout project: a GameCube/Wii program (DOL) or disc image
        (dtk-template), or a DS ROM (ds-decomp). Bytes are saved to a file first."""
        from ..forks import scratch_dsd, scratch_dtk

        head = Path(data).open("rb").read(0x440) if isinstance(data, (str, Path)) else bytes(data[:0x440])
        small = Path(data).read_bytes() if isinstance(data, (str, Path)) and Path(data).stat().st_size < 64 << 20 else data
        nds = scratch_dsd.is_nds(head)
        if not nds and scratch_dtk.disc_kind(head) is None and not (isinstance(small, (bytes, bytearray))
                                                                   and scratch_dtk.is_dol(small)):
            return None
        if isinstance(data, (str, Path)):
            return Path(data)
        uploads = self.workspace / ".decomp" / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        path = uploads / (Path(name).name or ("game.nds" if nds else "main.dol" if scratch_dtk.is_dol(data) else "game.iso"))
        path.write_bytes(data)
        return path

    @staticmethod
    def _asm_source(fn: split.RomFunction) -> str:
        out = [f"glabel {fn.name}"]
        for ln in fn.lines:
            if ln.vram in fn.labels and ln.vram != fn.vram:
                out.append(f".L{ln.vram:08X}:")
            out.append(f"/* {ln.rom:X} {ln.vram:08X} {ln.word:08X} */  {ln.mnemonic:<10} {ln.operands}".rstrip())
        out.append(f"endlabel {fn.name}")
        return "\n".join(out)

    @staticmethod
    def _parse_asm(name: str, text: str, segment: str) -> split.RomFunction:
        lines, labels = [], set()
        for raw in text.splitlines():
            m = split._INSN.match(raw)
            if m:
                rom, vram, word, mnem, ops = m.groups()
                lines.append(split.AsmLine(int(rom, 16), int(vram, 16), int(word, 16), mnem, ops))
                continue
            m = split._LABEL.match(raw)
            if m:
                labels.add(int(m.group(1).lstrip(".L"), 16))
        return split.RomFunction(name, lines[0].vram, lines[0].rom, segment, lines, Path(), labels)

    # ------------------------------------------------------------------ drafts

    def _seed_for(self, fn, proj) -> str | None:
        """m2c draft, made the first time someone opens the function (imports stay fast)."""
        if fn["seed"] is not None or not seeds.available():
            return fn["seed"]
        tc = profiles.effective_compiler(self.cfg(proj), fn)["toolchain"]
        asm = self._asm_text(fn, proj)
        data_asm = None
        if "jtbl_" in fn["asm"]:
            data_asm = "\n".join(self._jtbl_asm(proj, fn))
        ctx = self._context(proj["id"], fn["unit"])
        res = seeds.draft(asm, fn["name"], tc, context_header=ctx["header"] if ctx else "", data_asm=data_asm)
        draft = res.text
        text = res.annotated_text() if draft else ""
        with self.store.tx() as db:
            db.execute("UPDATE functions SET seed=? WHERE id=?", (text or None, fn["id"]))
            if draft:  # P4.6: the machine draft is a provenance-tagged candidate, not an anonymous overwrite
                self._note_candidate(db, fn, "m2c", draft, None)
        return text or None

    def draft_group(self, proj, fn, *, max_members: int = 5, max_insns: int = 600) -> str:
        """Bounded grouped drafting (plan P4.5): the function plus its nearest direct callers/callees,
        drafted together so inferred prototypes stay consistent across the group. Each member keeps its
        own unit's context (P4.5 step 3: unit boundaries are preserved) and is still compiled and
        evaluated independently against its own target by the judge — the group shares preparation,
        never verification. The drafts are recorded as provenance-tagged 'm2c-group' candidates."""
        if not seeds.available():
            return "m2c isn't installed: no drafts."
        if fn["vram"] is None:
            return f"{fn['name']} has no known address: nothing to group it with."
        xr = self.xrefs(proj)
        neighbours = sorted((xr["called_by"].get(fn["vram"]) or set()) | (xr["calls"].get(fn["vram"]) or set()))
        picked = [fn]
        for naddr in neighbours:
            if len(picked) >= max_members:
                break
            same = self.store.all("SELECT * FROM functions WHERE project_id=? AND vram=?", proj["id"], naddr)
            row = same[0] if len(same) == 1 else None  # several banks at one address: not a known neighbour
            if row is None or row["id"] == fn["id"] or row["status"] != "open":
                continue
            if sum(m["n_insns"] or 0 for m in picked) + (row["n_insns"] or 0) > max_insns:
                break  # bounded total work (P4.5 step 2)
            picked.append(row)
        tc = self.cfg(proj)["compiler"]["toolchain"]
        lines = [f"### Draft group for {fn['name']}: {len(picked)} function(s), "
                 f"{sum(m['n_insns'] or 0 for m in picked)} instructions "
                 f"(bounds: {max_members} members, {max_insns} insns)"]
        for m in picked:
            data_asm = "\n".join(self._jtbl_asm(proj, m)) if "jtbl_" in (m["asm"] or "") else None
            mctx = self._context(proj["id"], m["unit"])
            res = seeds.draft(self._asm_text(m, proj), m["name"], tc,
                              context_header=mctx["header"] if mctx else "", data_asm=data_asm)
            if res.text:
                with self.store.tx() as db:
                    self._note_candidate(db, m, "m2c-group", res.text, None)
                lines.append(f"## {m['name']} (context {res.context_status}, {res.outcome})\n```c\n{res.text}\n```")
            else:
                lines.append(f"## {m['name']}: no draft ({res.outcome})")
        return "\n\n".join(lines)

    def _jtbl_asm(self, proj, fn) -> list[str]:
        out = [".section .rodata"]
        read = self.reader(proj, fn)
        syms = self.symbols(proj["id"])
        for m in sorted(set(re.findall(r"jtbl_([0-9A-F]{8})", fn["asm"]))):
            addr = int(m, 16)
            sym = syms.by_addr.get(addr)
            size = sym.size if sym and sym.size else 0
            data = read(addr, size) if size else None
            if not data:
                continue
            out.append(f"dlabel {syms.label(addr)}")
            fmt = ">I" if self.cfg(proj).get("big_endian", True) else "<I"
            for (w,) in struct.iter_unpack(fmt, data[:len(data) // 4 * 4]):
                out.append(f".word .L{w:08X}" if fn["vram"] <= w < fn["vram"] + fn["size"] else f".word 0x{w:08X}")
        return out

    def get_context(self, project: str | None, function: str | None = None) -> str:
        self._rom_only(project, "get_context")
        return super().get_context(project, function)

    def _brief(self, fn, agent, header):
        if self.cfg(self.store.one("SELECT config FROM projects WHERE id=?", fn["project_id"])).get("kind") == "fork":
            return self.forks.brief(fn, agent, header)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        if self.is_rom(proj) and fn["status"] != "matched" and not fn["best_source"] and fn["seed"] is None:
            with self._seed_lock:
                self._seed_for(self._function(fn["name"], proj["slug"]), proj)
            fn = self._function(fn["name"], proj["slug"])
        return super()._brief(fn, agent, header)

    # ------------------------------------------------------------------ cross references

    def xrefs(self, proj) -> dict:
        """calls / called_by / uses / used_by, all keyed by address."""
        if proj["id"] in self._xrefs:
            return self._xrefs[proj["id"]]
        funcs = self.store.all("SELECT name, vram, size, relocs FROM functions WHERE project_id=?", proj["id"])
        starts = {f["vram"] for f in funcs}
        calls, called_by, uses, used_by = {}, {}, {}, {}
        for f in funcs:
            for off, rtype, addr in json.loads(f["relocs"]):
                if f["vram"] <= addr < f["vram"] + f["size"]:
                    continue
                if addr in starts:
                    calls.setdefault(f["vram"], set()).add(addr)
                    called_by.setdefault(addr, set()).add(f["vram"])
                elif rtype not in (match.R_MIPS_26, 10):  # 10: a Thumb call (GBA) to something not known as a function
                    uses.setdefault(f["vram"], set()).add(addr)
                    used_by.setdefault(addr, set()).add(f["vram"])
        self._xrefs[proj["id"]] = {"calls": calls, "called_by": called_by, "uses": uses, "used_by": used_by}
        return self._xrefs[proj["id"]]

    GB_NOTES = (
        "### Game Boy (SDCC via GBDK-2020)",
        "- **Define the function with its own name** (e.g. `void func_000b58(void) { }`): the compiled "
        "object must contain the symbol the app is judging, or the compile is reported as an error.",
        "- Calling convention (`sdcccall(1)`): byte arguments arrive in A, E, B, D, C; the return value "
        "leaves in A. Write C and let the target assembly tell you which registers it used.",
        "- Hardware registers and any absolute memory are **volatile pointers**: "
        "`*(volatile u8 *)0xFFF2 = v;` compiles to `ldh ($ff00+$f2), a`, `*(volatile u8 *)0x2100 = v;` "
        "compiles to `ld ($2100), a`, and `return *(volatile u8 *)0xC000;` reads a RAM byte.",
        "- To call another function, declare it and call it by name (`void func_004006(void);` … "
        "`func_004006();`) — the ROM's own address is substituted, so the call is byte-exact.",
        "- SDCC turns a call in tail position into `jp`; if the target does `call … ; ret`, give the "
        "call something to do afterwards (a following statement, or return a value).",
        "- There is no m2c draft for SM83: the target assembly is the specification.",
        "- **Matches must come from C**: inline assembly (`__asm`), `__naked`, pragmas and `#include <…>` "
        "are rejected by the source policy, exactly as on every other platform.",
    )

    def _extra_brief(self, fn, proj) -> list[str]:
        if not self.is_rom(proj):
            return []
        shared = self._extra_brief_shared(fn, proj)
        if (self.cfg(proj).get("arch") or "").lower() == "sm83":
            # Game Boy agents need the calling convention and the register idioms: nothing about SM83 is
            # guessable from other platforms, and the lane has no machine draft to start from.
            return self._gb_brief(fn, proj) + shared
        return shared

    def _gb_brief(self, fn, proj) -> list[str]:
        return ["", *self.GB_NOTES]

    def _extra_brief_shared(self, fn, proj) -> list[str]:
        syms = self.symbols(proj["id"])
        x = self.xrefs(proj)
        lines = ["", "### Where it lives",
                 f"address 0x{fn['vram']:08X}, ROM offset 0x{fn['rom_offset']:X}, segment {fn['unit']}"]
        calls = sorted(x["calls"].get(fn["vram"], ()))
        callers = sorted(x["called_by"].get(fn["vram"], ()))
        if calls:
            lines.append("calls: " + ", ".join(syms.label(a) for a in calls[:30]))
        if callers:
            lines.append("called by: " + ", ".join(syms.label(a) for a in callers[:30]))
        used = sorted(x["uses"].get(fn["vram"], ()))
        if used:
            lines += ["", "### Data it uses (declare what you need as extern in your source, or in context.h)"]
            prev = {r["name"]: r for r in self.store.all("SELECT name, kind, size, preview FROM symbols WHERE project_id=?", proj["id"])}
            for a in used[:40]:
                name, off = syms.name_at(a)
                r = prev.get(name)
                desc = f"{r['kind']}, {r['size']} bytes" if r else "unknown size"
                if r and r["preview"]:
                    desc += f": {r['preview']}"
                lines.append(f"- {syms.label(a)} (0x{a:08X}; {desc})")
        twin = self._twin_lines(proj, fn) if fn["status"] != "matched" else []
        lines += twin
        alike = [] if twin else [(r, sim) for r, sim in self.similar(proj, fn, top=3) if r["status"] == "matched"]
        if alike:
            lines += ["", "### Looks like (already matched; read with get_function)",
                      ", ".join(f"{r['name']} ({sim * 100:.0f}% similar)" for r, sim in alike)]
            r, sim = alike[0]
            src = self.store.one("SELECT matched_source FROM functions WHERE project_id=? AND name=?", proj["id"], r["name"])
            if sim >= 0.8 and src and src["matched_source"] and len(src["matched_source"]) <= 3000:
                # nearly the same code: its matching C is the best starting point there is (saves a tool call)
                lines += [f"Matching C of {r['name']} ({sim * 100:.0f}% similar): start from this",
                          "```c", src["matched_source"].strip(), "```"]
        lines += ["", "Names: use the names shown here (or rename_symbol to give an address a real name). "
                      "Your code is linked at this function's address and must reproduce the ROM bytes exactly."]
        return lines

    def similar(self, proj, fn, top: int = 5) -> list[tuple[dict, float]]:
        """Functions whose instructions look like `fn`'s, matched ones first: [(row, similarity)].
        Uses the shared per-project feature index (P6.2): features are built once per function revision and
        target digest, so a repeated query rebuilds nothing."""
        if fn["target"] is None:
            return []
        index = retrieval.feedback.similarity_index(self, proj)
        arch = proj["arch"] or "mips"
        mode = "big" if proj["big_endian"] else "little"
        rows = {r["id"]: r for r in self.store.all(
            "SELECT id, name, vram, size, n_insns, status, best_score, target, relocs, rom_offset, fingerprint, "
            "revision, unit, (matched_source IS NOT NULL AND matched_source != '') AS has_source "
            "FROM functions WHERE project_id=?", proj["id"])}
        me = rows.get(fn["id"])
        if me is None or not me["target"]:
            return []
        for fid, r in rows.items():
            if not r["target"]:
                continue
            try:  # a function that cannot be disassembled simply stays out of the index
                index.features_for(fid, r["revision"], snapshots.target_digest(r), arch, mode, r["target"],
                                   addr=r["vram"] or 0)
            except Exception:
                continue
        try:
            if index.features(fn["id"]).length < 4:  # tiny functions all look alike
                return []
        except KeyError:
            return []
        verified = {r["function_id"] for r in self.store.all(
            "SELECT v.function_id FROM verification_receipts v JOIN functions f ON f.id=v.function_id "
            "WHERE f.project_id=?", proj["id"])}
        cfg = self.cfg(proj)
        per_function = bool(cfg.get("compiler_ranges") or cfg.get("compiler_functions"))

        def compiler_of(r) -> str | None:
            if not per_function:
                return None
            try:
                return json.dumps(profiles.effective_compiler(cfg, r), sort_keys=True)
            except Exception:
                return None

        infos = {fid: similarity.CandidateInfo(
                     key=fid, source_available=r["status"] == "matched" and bool(r["has_source"]),
                     verified=fid in verified, compiler_profile=compiler_of(r))
                 for fid, r in rows.items() if fid != fn["id"]}
        target_info = similarity.CandidateInfo(key=fn["id"], compiler_profile=compiler_of(me))
        ranked = [(rows[m.key], m.similarity, m.rank_score)
                  for m in index.search(fn["id"], arch, mode, top=top * 2, infos=infos, target_info=target_info)
                  if m.key in rows]
        # matched first, then the usability-adjusted rank (P6.4), then raw similarity
        ranked.sort(key=lambda x: (x[0]["status"] != "matched", -x[2], -x[1]))
        return [(row, sim) for row, sim, _ in ranked[:top]]

    def find_similar(self, project: str | None, function: str, limit: int = 5) -> str:
        if (fork := self.fork_slug(project)):
            return self.forks.find_similar(fork, function, limit)
        proj = self._project(project)
        fn = self._function(function, proj["slug"])
        hits = self.similar(proj, fn, top=max(1, min(int(limit), 15)))
        if not hits:
            return f"No look-alikes found for {fn['name']}."
        lines = [f"Functions that look like {fn['name']}:"]
        for r, sim in hits:
            state = "matched" if r["status"] == "matched" else f"best {r['best_score']}" if r["best_score"] is not None else "not started"
            lines.append(f"- {r['name']}  {sim * 100:.0f}% similar  {state}  ({r['n_insns']} insns, 0x{r['vram']:08X})")
        best = next((r for r, _ in hits if r["status"] == "matched"), None)
        if best is not None:
            src = self.store.one("SELECT matched_source FROM functions WHERE project_id=? AND name=?", proj["id"], best["name"])
            if src and src["matched_source"]:
                lines += ["", f"Matching C of {best['name']}:", "```c", src["matched_source"].strip()[:4000], "```"]
        # A.14/P10.5: with retrieval routes enabled, the holdout leakage check runs before any
        # retrieval candidate is surfaced; a leaked answer's surface is blocked with the reason shown.
        blocked = retrieval.feedback.surface_leaks(self, proj, fn, config_from(proj))
        if blocked:
            lines += ["", "### Retrieval candidates — blocked (holdout leakage)"]
            for leak in blocked:
                lines.append(f"- {leak.answer_id}: {leak.note}")
            return "\n".join(lines)
        return "\n".join(self._retrieval_brief(proj, fn, lines))

    # --------------------------------------------------- retrieval routes (P10.4/P10.5)
    # Candidates only: retrieval suggests, compile_and_diff decides. Match state is never touched here.

    def _bsim_target_signature(self, fn, proj):
        """The BSim signature of one target function, from its stored assembly (the local pilot, P10.3).
        None when there is no assembly to sign."""
        if not (fn["asm"] or "").strip():
            return None
        cfg = json.loads(proj["config"])
        profile = (cfg.get("compiler") or {}).get("toolchain", "")
        return retrieval.BsimSignature.from_lines(
            fn["name"], fn["asm"].splitlines(), processor=proj["arch"] or "mips", mode="",
            profile=profile, program_digest=snapshots.target_digest(fn))

    def _retrieval_lookup(self, fn, proj) -> list:
        """(route_step, lane_result) for every enabled retrieval step, in route order: the result is
        the lane's answer even when empty/skipped, so the brief can explain *why* nothing came back.
        Off by default: returns [] before touching any lane or index."""
        config = config_from(proj)
        if not (config.function_id_retrieval or config.bsim_retrieval):
            return []
        registry = self.load_reference_sources(proj["id"])
        plan = retrieval.route_plan(config, registry, bsim=self.retrieval_bsim)
        fid_index = self.retrieval_fid or retrieval.FunctionIdIndex(registry)
        rows: list = []
        for step in plan:
            if step.name not in ("function_id", "bsim") or not step.enabled:
                continue
            if step.name == "function_id":
                result = retrieval.FidLane(fid_index, registry, config).lookup(
                    snapshots.target_digest(fn), proj["arch"] or "mips", "")
                rows.append((step, result))
            elif self.retrieval_bsim is not None:
                target = self._bsim_target_signature(fn, proj)
                result = (retrieval.BsimLane(self.retrieval_bsim, registry, config)
                          .query(target) if target is not None else None)
                rows.append((step, result))
        return rows

    def retrieval_candidates(self, fn, project) -> list:
        """The routed retrieval candidate list for one function (plan P10.4): empty when the project
        has no retrieval route enabled or nothing is admitted. One dict per candidate,
        {"route", "candidate", "requires_verification": True} — retrieval never touches match state."""
        proj = project if isinstance(project, dict) else self._project(project)
        out = []
        for step, result in self._retrieval_lookup(fn, proj):
            for candidate in (result.candidates if result is not None else []):
                out.append({"route": step.name, "candidate": candidate,
                            "requires_verification": True})
        return out

    def retrieval_leakage_check(self, slug: str, answers: dict) -> list:
        """The project's admitted corpus against benchmark answers (P10.5/B-05): flag any answer
        accidentally present in the retrieval corpus, from the engine's persisted registry."""
        proj = self._project(slug)
        return self.load_reference_sources(proj["id"]).leakage_check(answers)

    def _retrieval_brief(self, proj, fn, lines: list) -> list:
        """Append routed retrieval candidates to a find_similar brief (plan P10.4). Every candidate
        is labelled requires-verification. With every flag off (the default) the brief is exactly the
        pre-retrieval text."""
        config = config_from(proj)
        if not (config.function_id_retrieval or config.bsim_retrieval):
            return lines
        registry = self.load_reference_sources(proj["id"])
        plan = retrieval.route_plan(config, registry, bsim=None)
        lookups = {step.name: result for step, result in self._retrieval_lookup(fn, proj)}
        steps = [s for s in plan if s.name in ("function_id", "bsim")]
        if not any(s.enabled for s in steps):
            lines += ["", "### Retrieval routes (disabled)",
                      *(f"- {s.name}: {s.reason}" for s in steps)]
            return lines
        lines += ["", "### Retrieval candidates — requires verification (compile & diff before trusting)"]
        for step in steps:
            if not step.enabled:
                lines.append(f"- {step.name}: {step.reason}")
                continue
            result = lookups.get(step.name)
            if result is None:
                lines.append(f"- {step.name}: enabled, but no local index is loaded yet")
                continue
            if not result.ok():
                lines.append(f"- {step.name}: {result.reason}")
                continue
            if step.name == "function_id":
                if not result.candidates:
                    lines.append("- function_id: no known-library hits")
                for c in result.candidates:
                    label = c.suggested_name or c.hit.name
                    src = c.source_location or "no reusable source (evidence only)"
                    lines.append(f"- [function_id] {label} ({c.hit.library} {c.hit.corpus_version}) "
                                 f"source {src} — REQUIRES VERIFICATION")
            else:
                if not result.candidates:
                    lines.append("- bsim: no similar reference functions above the floor")
                for c in result.candidates:
                    if c.evidence_only:
                        lines.append(f"- [bsim] {c.key} ({c.similarity * 100:.0f}% similar) — evidence only, "
                                     "no reusable source — REQUIRES VERIFICATION")
                    else:
                        lines.append(f"- [bsim] {c.key} ({c.similarity * 100:.0f}% similar) source "
                                     f"{c.source_location} — REQUIRES VERIFICATION")
        return lines

    # ------------------------------------------------------------------ agent tools for ROMs

    def list_functions(self, project: str | None, status: str = "open", limit: int = 40) -> str:
        if (fork := self.fork_slug(project)):
            return self.forks.list_functions(fork, status, limit)
        proj = self._project(project)
        where = "" if status == "all" else " AND f.status=?"
        args = [proj["id"]] + ([] if status == "all" else [status])
        rows = self.store.all(
            f"SELECT f.name, f.vram, f.n_insns, f.status, f.best_score, a.name AS claimed_by FROM functions f "
            f"LEFT JOIN claims c ON c.function_id=f.id LEFT JOIN agents a ON a.id=c.agent_id "
            f"WHERE f.project_id=?{where} ORDER BY f.vram LIMIT ?", *args, limit)
        if not rows:
            return f"No {status} functions."
        return "\n".join(f"- {r['name']} @0x{r['vram']:08X}: {r['n_insns']} insns, {r['status']}"
                         + (f", best score {r['best_score']}" if r["best_score"] is not None else "")
                         + (f", claimed by {r['claimed_by']}" if r["claimed_by"] else "") for r in rows)

    def read_data(self, project: str | None, what: str, length: int = 64) -> str:
        self._rom_only(project, "read_data")
        proj = self._project(project)
        syms = self.symbols(proj["id"])
        addr = _address(what)
        if addr is None:
            addr = syms.resolve(what.strip())
        if addr is None:
            raise NetworkError(f"`{what}` is not an address or a known symbol")
        length = max(4, min(int(length), 512))
        data = self.reader(proj)(addr, length)
        head = f"{syms.label(addr)} at 0x{addr:08X}"
        if data is None:
            sym = syms.by_addr.get(addr)
            if sym and sym.kind == "bss":
                return f"{head}: uninitialized data (.bss), {sym.size} bytes; it starts as zeros at boot."
            return f"{head}: not in a loaded part of the ROM (maybe .bss, or a segment that isn't split yet)."
        out = [head]
        for i in range(0, len(data), 16):
            chunk = data[i:i + 16]
            text = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
            out.append(f"  {addr + i:08X}: {chunk.hex(' '):<47}  {text}")
        e = ">" if self.cfg(proj).get("big_endian", True) else "<"
        words = [w for (w,) in struct.iter_unpack(e + "I", data[:len(data) // 4 * 4])]
        ptrs = [f"+0x{4 * i:X}: {syms.label(w)}" for i, w in enumerate(words) if 0x80000400 <= w < 0x80800000]
        if ptrs:
            out.append("words that look like addresses: " + ", ".join(ptrs[:16]))
        floats = [struct.unpack(e + "f", struct.pack(e + "I", w))[0] for w in words[:8]]
        if any(1e-6 < abs(f) < 1e7 for f in floats):
            out.append("as f32: " + ", ".join(f"{f:g}" for f in floats))
        return "\n".join(out)

    def split_function(self, agent: Agent, project: str | None, function: str, at: str, new_name: str = "") -> str:
        with self.store.lock:
            pid = self._project(project)["id"]
            self._require_not_rescanning(pid)
            self._active_attempts[pid] = self._active_attempts.get(pid, 0) + 1
        try:
            return self._split_function_active(agent, project, function, at, new_name)
        finally:
            with self.store.lock:
                self._active_attempts[pid] -= 1

    def _split_function_active(self, agent, project, function, at, new_name):
        self._rom_only(project, "split_function")
        link = self._link(project)
        if link is not None:
            name = self._function(function, project)["name"]
            text = link.call("/collab/split", agent, function=name, at=at, new_name=new_name)
            if text.startswith("✅"):
                link.sync()
            return text
        fn = self._function(function, project)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        if not self.is_rom(proj):
            raise NetworkError("split_function is for ROM projects")
        if fn["status"] == "matched":
            raise NetworkError(f"{fn['name']} is matched; it can't be split")
        m = re.match(r"^(?:0x)?([0-9A-Fa-f]{2,8})$", str(at).strip())  # `at` is always an address
        if not m:
            raise NetworkError("`at` must be an address like 0x80001234")
        addr = int(m.group(1), 16)
        new_name = (new_name or f"func_{addr:08X}").strip()
        syms = self.symbols(proj["id"])
        if not IDENT.match(new_name) or new_name in syms.by_name:
            raise NetworkError(f"`{new_name}` is not a free C identifier")
        if self.cfg(proj).get("platform") == "gba":
            return self._split_gba(agent, proj, fn, addr, new_name, syms)
        if self.cfg(proj).get("platform") in ("gb", "gbc"):
            return self._split_gb(agent, proj, fn, addr, new_name, syms)
        rf = self._parse_asm(fn["name"], fn["asm"], fn["unit"])
        try:
            head, tail = split.split_function(rf, addr, new_name)
        except ValueError as e:
            raise NetworkError(str(e)) from None
        syms.add(Symbol(new_name, addr, tail.size, "func"))
        old = syms.by_name.get(fn["name"])
        if old is not None:
            old.size = head.size
        th, tt = self._make_target(proj, head, syms), self._make_target(proj, tail, syms)
        with self.store.tx() as db:
            db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL, best_score=NULL, "
                       "revision=revision+1 WHERE id=?", (head.size, len(head.lines), th.code, json.dumps(th.to_json()),
                                                          self._asm_source(head), fn["id"]))
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (proj["id"], new_name, tail.size, len(tail.lines), tt.code,
                                                        json.dumps(tt.to_json()), self._asm_source(tail), None,
                                                        fn["unit"], addr, tail.rom))
            db.execute("UPDATE symbols SET size=? WHERE project_id=? AND name=?", (head.size, proj["id"], fn["name"]))
            db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                       (proj["id"], new_name, addr, tail.size, "func"))
            self._event(db, proj["id"], agent.id, "split", f"{fn['name']} / {new_name}")
        self._xrefs.pop(proj["id"], None)
        return (f"✅ split: {fn['name']} is now {len(head.lines)} instructions; {new_name} at 0x{addr:08X} has "
                f"{len(tail.lines)}. Continue with {fn['name']} (get_function shows its new assembly).")

    # ------------------------------------------------------------------ boundary merge (C.4)

    def merge_function(self, agent: Agent, fragment: str, continues: str, project: str | None = None) -> str:
        """The reviewed reverse of split_function (plan C.4): `fragment` is reassembled onto `continues`
        — its bytes, assembly and relocations — the fragment row is deleted and the symbols updated.
        A merge requires the recorded merge_proposals row (propose_merge's review evidence, consumed on
        success), true adjacency, the fragment shape, and that neither function is matched. Refused
        merges change nothing and leave the proposal row for review."""
        self._rom_only(project, "merge_function")
        if self._link(project) is not None:
            raise NetworkError("in a team room only the host's app merges functions")
        frag = self._function(fragment, project)
        cont = self._function(continues, project)
        if frag["project_id"] != cont["project_id"]:
            raise NetworkError("both functions must be in the same project")
        if frag["status"] == "matched" or cont["status"] == "matched":
            raise NetworkError("matched functions can't be merged")
        proj = self.store.one("SELECT * FROM projects WHERE id=?", frag["project_id"])
        if not self.is_rom(proj):
            raise NetworkError("merge_function is for ROM projects")
        if not _adjacent(frag, cont, self.cfg(proj).get("arch") or ""):
            raise NetworkError(f"{frag['name']} and {cont['name']} are not adjacent: no boundary to merge")
        if not _fragment_like(frag, (self.cfg(proj).get("arch") or "")):
            raise NetworkError(f"{frag['name']} doesn't look like a fragment (no live-in saved register at entry)")
        if self._merge_proposal(frag["id"], cont["id"]) is None:
            raise NetworkError(f"no reviewed merge proposal for {frag['name']} continuing {cont['name']}; "
                               f"propose_merge first — merges are never applied without review evidence")
        if self.cfg(proj).get("platform") == "gba":
            return self._merge_gba(agent, proj, frag, cont)
        if self.cfg(proj).get("platform") in ("gb", "gbc"):
            return self._merge_gb(agent, proj, frag, cont)
        return self._merge_rom(agent, proj, frag, cont)

    def _merge_rom(self, agent: Agent, proj, frag, cont) -> str:
        """Reassemble a disassembled pair: the fragment's parsed instruction lines follow the
        continuing function's, and the target bytes/relocations are rebuilt from the combined record."""
        rf_cont = self._parse_asm(cont["name"], cont["asm"], cont["unit"])
        rf_frag = self._parse_asm(frag["name"], frag["asm"], frag["unit"])
        combined = split.RomFunction(cont["name"], rf_cont.vram, rf_cont.rom, rf_cont.segment,
                                     rf_cont.lines + rf_frag.lines, rf_cont.asm_path,
                                     rf_cont.labels | rf_frag.labels)
        syms = self.symbols(proj["id"])
        gone = syms.by_name.pop(frag["name"], None)
        if gone is not None:
            syms.by_addr.pop(gone.address, None)
        target = self._make_target(proj, combined, syms)
        with self.store.tx() as db:
            db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL, best_score=NULL, "
                       "revision=revision+1 WHERE id=?",
                       (combined.size, len(combined.lines), target.code, json.dumps(target.to_json()),
                        self._asm_source(combined), cont["id"]))
            self._delete_function_rows(db, frag["id"])
            db.execute("DELETE FROM functions WHERE id=?", (frag["id"],))
            db.execute("UPDATE symbols SET size=? WHERE project_id=? AND name=?",
                       (combined.size, proj["id"], cont["name"]))
            db.execute("DELETE FROM symbols WHERE project_id=? AND name=?", (proj["id"], frag["name"]))
            self._consume_merge_proposals(db, frag["id"])
            self._event(db, proj["id"], agent.id, "merge", f"{frag['name']} into {cont['name']}")
        self._xrefs.pop(proj["id"], None)
        self._ops.pop(proj["id"], None)
        self._syms.pop(proj["id"], None)
        return (f"✅ merged: {frag['name']} is now the continuation of {cont['name']} "
                f"({len(combined.lines)} instructions, {combined.size} bytes). "
                f"Continue with {cont['name']} (get_function shows its reassembled assembly).")

    def _merge_gba(self, agent: Agent, proj, frag, cont) -> str:
        """Reassemble a GBA pair at the byte level (the reverse of _split_gba): concatenated bytes,
        re-offset relocations, joined assembly."""
        off = len(cont["target"] or b"")
        target = (cont["target"] or b"") + (frag["target"] or b"")
        refs = json.loads(cont["relocs"]) + [[r[0] + off, r[1], r[2]] for r in json.loads(frag["relocs"])]
        asm = (cont["asm"] or "").rstrip() + "\n" + (frag["asm"] or "").strip()
        size = len(target)
        n_insns = (cont["n_insns"] or 0) + (frag["n_insns"] or 0)
        with self.store.tx() as db:
            db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL, best_score=NULL, "
                       "revision=revision+1 WHERE id=?",
                       (size, max(1, n_insns), target, json.dumps(refs), asm, cont["id"]))
            self._delete_function_rows(db, frag["id"])
            db.execute("DELETE FROM functions WHERE id=?", (frag["id"],))
            db.execute("UPDATE symbols SET size=? WHERE project_id=? AND name=?", (size, proj["id"], cont["name"]))
            db.execute("DELETE FROM symbols WHERE project_id=? AND name=?", (proj["id"], frag["name"]))
            self._consume_merge_proposals(db, frag["id"])
            self._event(db, proj["id"], agent.id, "merge", f"{frag['name']} into {cont['name']}")
        self._xrefs.pop(proj["id"], None)
        self._ops.pop(proj["id"], None)
        self._syms.pop(proj["id"], None)
        return (f"✅ merged: {frag['name']} is now the continuation of {cont['name']} "
                f"({n_insns} instructions, {size} bytes). Continue with {cont['name']}.")

    def _merge_gb(self, agent: Agent, proj, frag, cont) -> str:
        """Reassemble a Game Boy pair at the byte level (the reverse of `_split_gb`): concatenated bytes,
        re-offset relocations, joined disassembly. The two ranges are contiguous in the ROM, so nothing
        has to be re-parsed -- which is why the MIPS `_merge_rom` path could not serve this lane."""
        off = len(cont["target"] or b"")
        target = (cont["target"] or b"") + (frag["target"] or b"")
        refs = (json.loads(cont["relocs"] or "[]")
                + [[r[0] + off, r[1], r[2]] for r in json.loads(frag["relocs"] or "[]")])
        asm = (cont["asm"] or "").rstrip() + "\n" + (frag["asm"] or "").strip()
        size = len(target)
        n_insns = (cont["n_insns"] or 0) + (frag["n_insns"] or 0)
        with self.store.tx() as db:
            db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL, best_score=NULL, "
                       "revision=revision+1 WHERE id=?",
                       (size, max(1, n_insns), target, json.dumps(refs), asm, cont["id"]))
            self._delete_function_rows(db, frag["id"])
            db.execute("DELETE FROM functions WHERE id=?", (frag["id"],))
            db.execute("UPDATE symbols SET size=? WHERE project_id=? AND name=?", (size, proj["id"], cont["name"]))
            db.execute("DELETE FROM symbols WHERE project_id=? AND name=?", (proj["id"], frag["name"]))
            self._consume_merge_proposals(db, frag["id"])
            self._event(db, proj["id"], agent.id, "merge", f"{frag['name']} into {cont['name']}")
        self._xrefs.pop(proj["id"], None)
        self._ops.pop(proj["id"], None)
        self._syms.pop(proj["id"], None)
        return (f"✅ merged: {frag['name']} is now the continuation of {cont['name']} "
                f"({n_insns} instructions, {size} bytes). Continue with {cont['name']}.")

    def _split_gba(self, agent: Agent, proj, fn, addr: int, new_name: str, syms: SymbolTable) -> str:
        """Cut a GBA function's bytes and assembly in two at `addr` (Thumb: a halfword boundary)."""
        off = addr - fn["vram"]
        if off >= fn["size"] and off % 2 == 0:  # code after the function that nobody found yet: make it a function
            return self._add_gba_function(agent, proj, fn, addr, new_name, syms)
        if off <= 0 or off >= fn["size"] or off % 2:
            raise NetworkError(f"0x{addr:08X} isn't inside {fn['name']} (0x{fn['vram']:08X}, {fn['size']} bytes)")
        head, tail = fn["target"][:off], fn["target"][off:]
        cut = re.search(rf"^(?:_|\.L){addr:08X}:|^\t(?:thumb|arm)_func_start \w*{addr:08X}\b", fn["asm"], re.M)
        asm_head, asm_tail = ((fn["asm"][:cut.start()], fn["asm"][cut.start():]) if cut else
                              (fn["asm"], f"\tthumb_func_start {new_name}\n@ split from {fn['name']}: see its assembly\n"))
        refs = json.loads(fn["relocs"])
        refs_head = [r for r in refs if r[0] < off]
        refs_tail = [[r[0] - off, r[1], r[2]] for r in refs if r[0] >= off]
        syms.add(Symbol(new_name, addr, len(tail), "func"))
        with self.store.tx() as db:
            db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL, best_score=NULL, "
                       "revision=revision+1 WHERE id=?", (len(head), max(1, len(head) // 2), head, json.dumps(refs_head),
                                                          asm_head, fn["id"]))
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (proj["id"], new_name, len(tail), max(1, len(tail) // 2), tail,
                                                        json.dumps(refs_tail), asm_tail, None, fn["unit"], addr,
                                                        fn["rom_offset"] + off))
            db.execute("UPDATE symbols SET size=? WHERE project_id=? AND name=?", (len(head), proj["id"], fn["name"]))
            db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                       (proj["id"], new_name, addr, len(tail), "func"))
            self._event(db, proj["id"], agent.id, "split", f"{fn['name']} / {new_name}")
        self._xrefs.pop(proj["id"], None)
        self._ops.pop(proj["id"], None)
        return (f"✅ split: {fn['name']} is now {len(head)} bytes; {new_name} at 0x{addr:08X} has {len(tail)}. "
                f"Continue with {fn['name']} (get_function shows its new assembly).")

    def _split_gb(self, agent: Agent, proj, fn, addr: int, new_name: str, syms: SymbolTable) -> str:
        """Cut a Game Boy function's bytes and disassembly in two at `addr` (an SM83 CPU address).

        A third of this project's rows are several routines (or routines plus their data) joined into one
        function by mgbdis' boundary guess. No C can match such a row, because a compiled candidate is one
        function per label -- but the row itself is fine: split it and each piece matches on its own. The
        stored disassembly is one instruction per line, so the split is exact: cut the bytes at the
        offset, count the instructions in the head with the SM83 decoder and cut the text there.
        """
        from ..rom import sm83

        off = addr - fn["vram"]
        if off <= 0 or off >= fn["size"]:
            raise NetworkError(f"0x{addr:04X} isn't inside {fn['name']} (0x{fn['vram']:04X}, {fn['size']} bytes)")
        head, tail = fn["target"][:off], fn["target"][off:]
        lines = fn["asm"].splitlines()

        def render(code: bytes, base: int) -> str:
            """mgbdis' text for the head when it is there, else a fresh disassembly of these bytes."""
            out, at = [], 0
            while at < len(code):
                text, ln = sm83.decode(code[at:], base + at)
                out.append("    " + text)
                at += max(1, ln)
            return "\n".join(out)

        # mgbdis separates code from data, so a joined row often carries text for its first routine only.
        # Keep the lines that belong to the head (one per instruction, capped by what is there) and render
        # the tail from its own bytes -- it was data to mgbdis, so there is nothing to carry over.
        n_head = max(1, len(list(sm83.disassemble(head, fn["vram"]))))
        n_tail = max(1, len(list(sm83.disassemble(tail, addr))))
        keep = min(n_head, len(lines))
        asm_head = "\n".join(lines[:keep]) if keep else render(head, fn["vram"])
        asm_tail = "\n".join(lines[keep:]) or render(tail, addr)
        refs = json.loads(fn["relocs"] or "[]")
        refs_head = [r for r in refs if r[0] < off]
        refs_tail = [[r[0] - off, r[1], r[2]] for r in refs if r[0] >= off]
        syms.add(Symbol(new_name, addr, len(tail), "func"))
        old = syms.by_name.get(fn["name"])
        if old is not None:
            old.size = len(head)
        with self.store.tx() as db:
            db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL, best_score=NULL, "
                       "revision=revision+1 WHERE id=?", (len(head), n_head, head, json.dumps(refs_head), asm_head, fn["id"]))
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (proj["id"], new_name, len(tail), n_tail, tail,
                                                        json.dumps(refs_tail), asm_tail, None, fn["unit"], addr,
                                                        fn["rom_offset"] + off))
            db.execute("UPDATE symbols SET size=? WHERE project_id=? AND name=?", (len(head), proj["id"], fn["name"]))
            db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                       (proj["id"], new_name, addr, len(tail), "func"))
            self._event(db, proj["id"], agent.id, "split", f"{fn['name']} / {new_name}")
        self._xrefs.pop(proj["id"], None)
        self._ops.pop(proj["id"], None)
        return (f"✅ split: {fn['name']} is now {len(head)} bytes ({n_head} instructions); {new_name} at "
                f"0x{addr:04X} has {len(tail)} bytes ({n_tail} instructions). Continue with {fn['name']}.")

    def _add_gba_function(self, agent: Agent, proj, prev, addr: int, new_name: str, syms: SymbolTable) -> str:
        from ..rom import gba, gba_candidates

        nxt = self.store.one("SELECT vram FROM functions WHERE project_id=? AND vram>? ORDER BY vram LIMIT 1",
                             proj["id"], prev["vram"])
        rom, _ = self._rom(proj)
        off, limit = addr - gba.BASE, (nxt["vram"] - gba.BASE) if nxt else len(rom)
        if not off < limit or not gba_candidates.plausible_function(rom, off):
            raise NetworkError(f"0x{addr:08X} doesn't look like the start of a function")
        literals = gba_candidates.literal_words(rom, [addr])
        end = min(next(gba_candidates.bodies(rom, [addr], literals))[1], limit)
        while end + 4 <= limit and end in literals:
            end += 4
        code = rom[off:end]
        syms.add(Symbol(new_name, addr, len(code), "func"))
        with self.store.tx() as db:
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (proj["id"], new_name, len(code), max(1, len(code) // 2), code,
                                                        json.dumps(gba._refs(code, addr, literals, off)),
                                                        f"\tthumb_func_start {new_name}\n@ found by an agent after {prev['name']}\n",
                                                        None, prev["unit"], addr, off))
            db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                       (proj["id"], new_name, addr, len(code), "func"))
            self._event(db, proj["id"], agent.id, "split", f"{prev['name']} / {new_name}")
        self._xrefs.pop(proj["id"], None)
        self._ops.pop(proj["id"], None)
        return f"✅ new function {new_name} at 0x{addr:08X}, {len(code)} bytes."

    def rename_symbol(self, agent: Agent, project: str | None, old: str, new: str, message: str = "") -> str:
        with self.store.lock:
            pid = self._project(project)["id"]
            self._require_not_rescanning(pid)
            self._active_attempts[pid] = self._active_attempts.get(pid, 0) + 1
        try:
            return self._rename_symbol_active(agent, project, old, new, message)
        finally:
            with self.store.lock:
                self._active_attempts[pid] -= 1

    def _rename_symbol_active(self, agent, project, old, new, message):
        self._rom_only(project, "rename_symbol")
        proj = self._project(project)
        if not self.is_rom(proj):
            return super().rename_symbol(agent, project, old, new, message)
        link = self._link(project)
        if link is not None:
            text = link.call("/collab/rename", agent, old=old, new=new, message=message)
            if text.startswith("✅"):
                link.sync()
            return text
        old, new = old.strip(), new.strip()
        if not (IDENT.match(old) and IDENT.match(new)):
            raise NetworkError("old and new must be C identifiers")
        syms = self.symbols(proj["id"])
        addr = syms.resolve(old)
        if addr is None:
            raise NetworkError(f"`{old}` is not a symbol of this ROM: use a name from the assembly (func_/D_...) "
                               f"or one given earlier")
        taken = syms.resolve(new)
        if taken is not None and taken != addr:
            raise NetworkError(f"`{new}` already names 0x{taken:08X}")
        word = re.compile(rf"\b{re.escape(old)}\b")
        trial = SymbolTable([Symbol(s.name, s.address, s.size, s.kind) for s in syms.by_name.values()])
        if old in trial.by_name:
            trial.rename(old, new)
        else:
            fnrow = self.store.one("SELECT size FROM functions WHERE project_id=? AND vram=?", proj["id"], addr)
            current, _ = syms.name_at(addr)
            base = syms.by_name.get(current) if current != f"D_{addr:08X}" else None
            trial.add(Symbol(new, addr, fnrow["size"] if fnrow else (base.size if base and base.address == addr else 0),
                             "func" if fnrow else (base.kind if base else "data")))
        units = self._unit_contexts(proj["id"])
        current_ctx = {u: self._context(proj["id"], u) for u in ({""} | units)}
        changed = {u: word.sub(new, c["header"]) for u, c in current_ctx.items() if word.search(c["header"])}
        matched = self.store.all("SELECT * FROM functions WHERE project_id=? AND status='matched'", proj["id"])
        rewritten = {r["id"]: word.sub(new, r["matched_source"]) for r in matched}
        renamed_rows = [dict(r) | {"name": new if r["name"] == old else r["name"]} for r in matched]
        header_for = lambda fn: changed.get(self._effective_unit(fn, units),
                                            current_ctx[self._effective_unit(fn, units)]["header"])
        self._sym_override[proj["id"]] = trial
        try:
            broken = self.regression_test(proj, None, sources=rewritten, only=renamed_rows, header_for=header_for)
        finally:
            self._sym_override.pop(proj["id"], None)
        if broken:
            listing = "\n".join(f"- {n}: {why}" for n, why in broken[:10])
            return f"❌ REJECTED: renaming breaks {len(broken)} matched function(s).\n{listing}"
        msg = message.strip() or f"rename {old} -> {new}"
        with self.store.tx() as db:
            if db.execute("UPDATE symbols SET name=? WHERE project_id=? AND name=?", (new, proj["id"], old)).rowcount == 0:
                s = trial.by_name[new]
                db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                           (proj["id"], new, s.address, s.size, s.kind))
            # The function `old` names; by address only when that is unambiguous (Game Boy banks share CPU
            # addresses: another bank's function at the same address must keep its name)
            row = db.execute("SELECT id FROM functions WHERE project_id=? AND name=?", (proj["id"], old)).fetchone()
            if row is None:
                at = db.execute("SELECT id FROM functions WHERE project_id=? AND vram=?", (proj["id"], addr)).fetchall()
                row = at[0] if len(at) == 1 else None
            if row is not None:
                db.execute("UPDATE functions SET name=? WHERE id=?", (new, row["id"]))
            for u, header in changed.items():
                version = self._next_version(db, proj["id"])
                db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                           "VALUES(?,?,?,?,?,?,?)", (proj["id"], version, header, agent.id, msg, time.time(), u))
            for fid, src in rewritten.items():
                db.execute("UPDATE functions SET matched_source=?, best_source=? WHERE id=?", (src, src, fid))
            for r in db.execute("SELECT id, best_source FROM functions WHERE project_id=? AND status='open' "
                                "AND best_source IS NOT NULL", (proj["id"],)).fetchall():
                db.execute("UPDATE functions SET best_source=? WHERE id=?", (word.sub(new, r["best_source"]), r["id"]))
            self._event(db, proj["id"], agent.id, "rename", f"{old} → {new}")
        self._syms.pop(proj["id"], None)
        return f"✅ 0x{addr:08X} is now `{new}` (was `{old}`); {len(rewritten)} matched sources updated, all still match."

    # ------------------------------------------------------------------ rooms (see collab.py)

    def link_project(self, slug: str, info: dict, client=None, start: bool = True):
        from .collab import Link

        proj = self._project(slug)
        cfg = self.cfg(proj)
        cfg["link"] = info
        with self.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
        link = Link(self, slug, info["url"], info["room"], info["token"], info["member"], client=client)
        self.links[slug] = link
        if start:
            link.run()
        return link

    def restore_links(self) -> None:
        for p in self.store.all("SELECT slug, config FROM projects"):
            info = json.loads(p["config"]).get("link")
            if info and p["slug"] not in self.links:
                self.link_project(p["slug"], info)

    def unlink(self, slug: str) -> None:
        link = self.links.pop(slug, None)
        if link is not None:
            link.stop()
            try:
                link._req("POST", "/collab/leave", {})
            except NetworkError:
                pass
        proj = self._project(slug)
        cfg = self.cfg(proj)
        cfg.pop("link", None)
        with self.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))

    def _link(self, project: str | None):
        if not self.links:
            return None
        try:
            return self.links.get(self._project(project)["slug"])
        except NetworkError:
            return None

    def claim(self, agent: Agent, project: str | None = None, function: str | None = None, strategy: str = "easiest") -> str:
        if (fork := self.fork_slug(project)):
            return self.forks.claim(agent, fork, function, strategy)
        link = self._link(project)
        if link is None:
            return super().claim(agent, project, function, strategy)
        if function:
            function = self._function(function, project)["name"]
        # The host hands out the claim, this copy has to be able to take it too. The host is told what this copy
        # set aside (BIOS calls, hand-written assembly): its strategy claims leave those out unless a person gave
        # them back to agents there. A function this copy can't take anyway (an older host, which ignores the
        # list, or something matched here) is given straight back on the host, so the two never disagree about
        # who holds what, and from then on this copy picks by the strategy itself and asks for it by name: an
        # older host would only offer the next set-aside stub.
        proj = self._project(project)
        self._set_aside_once(proj)
        skip = self.set_aside_names(proj)
        refused, retried, pick_here = [], False, False
        for _ in range(ROOM_CLAIM_TRIES):
            try:
                if function is None and (pick_here or strategy in ("quick", "close")):
                    # a Focus in a room: the host may not know these strategies, so pick here and ask for it by name
                    name = self._room_focus_claim(link, agent, project, strategy)
                else:
                    name = link.claim(agent, function, strategy, skip=skip)
            except NetworkError as e:
                held = _HOST_HOLDS.match(str(e))
                if held is None or retried or self._holds(agent, held.group(1), project):
                    raise
                self._release_on_host(link, agent, held.group(1))  # a claim this copy never got: let it go
                retried = True
                continue
            if name is None:
                return "Nothing left to claim in this room — every open function is matched or someone is on it. 🎉"
            link.sync()
            try:
                fn = self.claim_row(agent, project, name, strategy)
            except NetworkError as e:
                self._release_on_host(link, agent, name)
                if function or str(e).startswith("you already hold"):
                    raise
                refused.append(f"{name} ({e})")
                pick_here = True
                continue
            return self._brief(fn, agent, header="CLAIMED")
        raise NetworkError("the room's host kept offering functions this copy can't take ("
                           + ("; ".join(refused[-3:]) or "it says you hold one this copy doesn't")
                           + "); they were given back. Call claim_function again, or name a function.")

    def _holds(self, agent: Agent, name: str, project: str | None) -> bool:
        try:
            fid = self._function(name, project)["id"]
        except NetworkError:
            return False
        return self.store.one("SELECT 1 FROM claims WHERE function_id=? AND agent_id=?", fid, agent.id) is not None

    @staticmethod
    def _release_on_host(link, agent: Agent, name: str) -> None:
        try:
            link.call("/collab/release", agent, function=name, notes="")
        except NetworkError:
            pass  # not held there any more (or unreachable: the host's claim expires on its own)

    def set_aside_names(self, proj) -> list[str]:
        """The functions this copy set aside as BIOS calls or hand-written assembly, smallest first (what a room
        member's claims ask the host to leave out)."""
        return [r["name"] for r in self.store.all("SELECT name FROM functions WHERE project_id=? AND status='asm' "
                                                  "ORDER BY n_insns, vram LIMIT ?", proj["id"], SKIP_MAX)]

    def follow_give_back(self, slug: str, name: str) -> None:
        """The room's host gave `name` back to agents by hand: this copy follows, so its helpers can take it too."""
        fn = self.store.one("SELECT id, status, meta FROM functions WHERE project_id=? AND name=?",
                            self._project(slug)["id"], name)
        if fn is None or fn["status"] != "asm":
            return
        meta = json.loads(fn["meta"] or "{}") if fn["meta"] else {}
        meta["for_agents"] = True
        with self.store.tx() as db:
            db.execute("UPDATE functions SET status='open', meta=? WHERE id=? AND status='asm'", (json.dumps(meta), fn["id"]))

    def _room_focus_claim(self, link, agent: Agent, project: str | None, strategy: str) -> str | None:
        """The first open function by `strategy` in this copy of the room that the host lets the agent have."""
        proj = self._project(project)
        cap = STRATEGY_INSNS_CAP.get(strategy, -1)
        rows = self.store.all(
            f"""SELECT f.name FROM functions f WHERE f.project_id=? AND f.status='open'
                AND f.id NOT IN (SELECT function_id FROM claims WHERE agent_id<>?)
                AND (? = -1 OR f.n_insns <= ?) {STRATEGY_WHERE.get(strategy, "")}
                ORDER BY {STRATEGIES.get(strategy) or STRATEGIES["easiest"]} LIMIT 12""", proj["id"], agent.id, cap, cap)
        for r in rows:
            try:
                return link.claim(agent, r["name"], "easiest")
            except NetworkError as e:  # someone in the room has it (this copy hadn't heard yet): the next one
                if "isn't reachable" in str(e):
                    raise
                continue
        return None

    def claim_many(self, agent: Agent, project: str | None, count: int, strategy: str = "easiest") -> str:
        """Several small functions at once (count 2-6): the next one by `strategy` and, when it's small, its
        neighbours in the same file. Most small functions match on the first try, so an agent writes them all and
        judges them in one compile_and_diff: a fraction of the turns. Big functions come alone."""
        count = max(1, min(int(count or 1), 6))
        if count == 1 or self._link(project) is not None:
            return self.claim(agent, project, None, strategy)
        proj = self._project(project)
        held = self.store.all("SELECT f.* FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=? "
                              "AND f.project_id=? ORDER BY f.vram, f.id", agent.id, proj["id"])
        if not held:
            text = self.claim(agent, project, None, strategy)  # the first, the usual way (with its "nothing left")
            held = self.store.all("SELECT f.* FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=? "
                                  "AND f.project_id=?", agent.id, proj["id"])
            if not held or held[0]["n_insns"] > BATCH_INSNS:
                return text
        first = held[0]
        rows = list(held)
        if first["n_insns"] <= BATCH_INSNS and len(rows) < count:
            rows += self._batch_more(agent, proj, first, count - len(rows))
        if len(rows) == 1:
            return self.claim(agent, project, None, strategy)
        names = [r["name"] for r in rows]
        fork = self.fork_slug(project)
        head = (f"## CLAIMED {len(rows)} small functions: {', '.join(names)}\n"
                f"Write all of them, then judge them in one call: compile_and_diff(function=\"{', '.join(names)}\", "
                f"source=<all {len(rows)} definitions>). Each is judged on its own and saved as soon as it matches; "
                "then fix what's left (one at a time is fine). release_function any you can't do.")
        briefs = [self.forks.brief(r, agent, "CLAIMED") if fork else self._brief(r, agent, "CLAIMED") for r in rows]
        return "\n\n---\n\n".join([head, *briefs])

    def _batch_more(self, agent: Agent, proj, first, n: int) -> list:
        """Up to n more small open functions next to `first` (same file, nearest address), claimed for the agent."""
        now = time.time()
        with self.store.tx() as db:
            self._require_not_rescanning(proj["id"])
            self._expire_claims(db)
            rows = db.execute(
                "SELECT f.* FROM functions f WHERE f.project_id=? AND f.status='open' AND f.boost >= 0 AND f.n_insns <= ? "
                "AND f.abandons = 0 AND f.unit=? AND f.id NOT IN (SELECT function_id FROM claims) "
                "ORDER BY ABS(COALESCE(f.vram, 0) - ?), f.id LIMIT ?",
                (proj["id"], BATCH_INSNS, first["unit"], first["vram"] or 0, n)).fetchall()
            for r in rows:
                db.execute("INSERT INTO claims VALUES(?,?,?,?)", (r["id"], agent.id, now, now + self.claim_ttl))
                self._event(db, proj["id"], agent.id, "claim", r["name"])
        return rows

    def attempt_many(self, agent: Agent, names: list[str], source: str, project: str | None = None) -> str:
        """compile_and_diff for several functions in one source: each judged on its own. A fork gets each
        definition one by one, as its file gets them. A ROM project compiles the translation unit once per
        compatible group (same project, same unit context) and evaluates every function against its own
        target — one compile instead of one per function (plan P2.2)."""
        fork = self.fork_slug(project)
        out, matched = [], 0
        if fork:
            for name in names:
                try:
                    one = self.forks.one_definition(fork, source, name, [n for n in names if n != name])
                    res = self.attempt(agent, name, one, project)
                except NetworkError as e:
                    res = {"text": f"❌ {e}", "matched": False}
                matched += bool(res.get("matched"))
                out.append(f"### {name}\n{res['text']}")
            return f"{matched} of {len(names)} matched.\n\n" + "\n\n".join(out)

        # ROM path: resolve, claim and snapshot every function first, then compile each compatible group once.
        self._require_budget(agent)
        groups: dict[tuple, list] = {}
        inserted: list[int] = []  # claims THIS batch created: released again if the batch cannot start
        try:
            for name in names:
                fn = self._function(name, project)
                proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
                now = time.time()
                with self.store.tx() as db:  # the same claim bookkeeping as attempt(): renew yours, never another's
                    self._expire_claims(db)
                    claim = db.execute("SELECT agent_id FROM claims WHERE function_id=?", (fn["id"],)).fetchone()
                    scratch = fn["status"] == "matched"
                    if not scratch:
                        if claim is None:
                            # claims held by this agent on OTHER functions of this batch are expected (claim_many
                            # pre-claims the batch); a claim outside the batch is the usual single-function conflict
                            held = db.execute("SELECT f.name FROM claims c JOIN functions f ON f.id=c.function_id "
                                              "WHERE c.agent_id=?", (agent.id,)).fetchall()
                            if any(h["name"] not in names for h in held):
                                raise NetworkError("you hold another claim; release it before working on a different function")
                            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + self.claim_ttl))
                            self._event(db, fn["project_id"], agent.id, "claim", fn["name"])
                            inserted.append(fn["id"])
                        elif claim["agent_id"] != agent.id:
                            raise NetworkError(f"{fn['name']} is claimed by {self._agent_name(claim['agent_id'])}")
                        else:
                            db.execute("UPDATE claims SET expires_at=? WHERE function_id=?", (now + self.claim_ttl, fn["id"]))
                ctx = self._context(proj["id"], fn["unit"])
                snap = snapshots.capture(fn, proj, ctx, symbols_digest=snapshots.symbol_table_digest(self.store, proj["id"]))
                # Complete compilation identity for grouping: project, context version, exact header content and
                # the function's effective compiler profile. Incompatible requests land in different groups
                # instead of sharing a compile (plan C-02, P2.2).
                compiler_id = json.dumps(profiles.effective_compiler(self.cfg(proj), fn), sort_keys=True, default=str)
                groups.setdefault((proj["id"], ctx["version"], ctx["header"], compiler_id), []).append(
                    (fn, proj, ctx, snap, scratch))
        except NetworkError:
            if inserted:
                with self.store.tx() as db:
                    for fid in inserted:
                        db.execute("DELETE FROM claims WHERE function_id=? AND agent_id=?", (fid, agent.id))
            raise

        compiles = 0
        for _, items in groups.items():
            fn0, proj0, ctx0, _, _ = items[0]
            with telemetry.span("batch.compile", functions=len(items)):
                result = self._compile(proj0, ctx0["header"], source, fn0)
            telemetry.count("batch.compiler_invocations")
            compiles += 1
            if not result.ok:  # a shared compile error: report once per function, keep per-function records
                host = not items[0][4] and self._directives(proj0, source, fn0).applied
                for fn, proj, ctx, snap, scratch in items:
                    # comments this copy couldn't build: the room's host may (see Network.attempt)
                    judged = self._second_judge(agent, fn, source, False, failed_here=True) if host else None
                    if judged is not None:
                        res = self._host_verdict(agent, fn, ctx, snap, source, judged, result.log.strip())
                        matched += bool(res.get("matched"))
                        out.append(f"### {fn['name']}\n{res['text']}")
                        continue
                    self._record(fn, agent, None, source, ctx["version"], snapshot=snap)
                    out.append(f"### {fn['name']}\n❌ COMPILE ERROR (context v{ctx['version']})\n{result.log.strip()}")
                continue
            for fn, proj, ctx, snap, scratch in items:
                res = self._judge(agent, fn, proj, ctx, snap, source, result, scratch)
                matched += bool(res.get("matched"))
                out.append(f"### {fn['name']}\n{res['text']}")
        return (f"{matched} of {len(names)} matched ({compiles} compile"
                f"{'s' if compiles != 1 else ''} for {len(names)} functions).\n\n" + "\n\n".join(out))

    def _yield_autopilot(self, function: str | None, project: str | None) -> None:
        """The autopilot holds a function only while its draft compiles: an agent that wants it waits for that."""
        if not function:
            return
        try:
            fid = self._function(function, project)["id"]
        except NetworkError:
            return
        for _ in range(100):
            held = self.store.one("SELECT a.name FROM claims c JOIN agents a ON a.id=c.agent_id WHERE c.function_id=?", fid)
            if not held or held["name"] != "autopilot":
                return
            time.sleep(0.2)

    def attempt(self, agent: Agent, function: str, source: str, project: str | None = None) -> dict:
        # Include scratch compiles of matched functions, which deliberately have no claim.
        with self.store.lock:
            pid = self._function(function, project)["project_id"]
            self._require_not_rescanning(pid)
            self._active_attempts[pid] = self._active_attempts.get(pid, 0) + 1
        try:
            return self._attempt_active(agent, function, source, project)
        finally:
            with self.store.lock:
                self._active_attempts[pid] -= 1

    def _attempt_active(self, agent: Agent, function: str, source: str, project: str | None = None) -> dict:
        self._yield_autopilot(function, project)
        if (fork := self.fork_slug(project)):
            return self.forks.attempt(agent, fork, function, source)
        link = self._link(project)
        if link is None:
            return super().attempt(agent, function, source, project)
        # In a room the host is the judge: _second_judge asks it from inside the normal acceptance path, at most
        # once per attempt, so a match it rejects never lands and its score is the one recorded
        fn = self._function(function, project)
        res = super().attempt(agent, function, source, project)
        if res["matched"]:
            res["text"] += "\nShared with the room: the host confirmed the match."
        elif (res["score"] is not None and fn["status"] != "matched" and not self._unknown_directives(fn, source)
              and (fn["best_score"] is None or res["score"] < fn["best_score"])):
            threading.Thread(target=lambda: self._report(link, agent, fn["name"], source, res["score"]),
                             daemon=True).start()
        return res

    def _second_judge(self, agent: Agent, fn, source: str, exact: bool, failed_here: bool = False) -> dict | None:
        """A room's host has the last word: asked about every exact result here, and about every source with
        decomp-* comments this copy can't apply (see directives), whose score here means little (the host's
        per-member rate limit bounds how often). A host match of a function the host cuts differently (its size
        isn't this copy's: it merged a false split, say) is no match here: this copy's function is other code."""
        directives = self._unknown_directives(fn, source) or failed_here
        if not exact and not directives:
            return None
        slug = self.store.one("SELECT slug FROM projects WHERE id=?", fn["project_id"])["slug"]
        link = self.links.get(slug)
        if link is None or link.fork:
            return None
        try:
            got = link.attempt(agent, fn["name"], source)
        except NetworkError as e:
            if "isn't reachable" not in str(e):  # the host answered, with a refusal
                known = not str(e).startswith("no function")
                return {"matched": False, "score": None, "text": (
                    f"❌ The room's host didn't take this attempt ({e}). NOT saved as a match." if known else
                    f"❌ The room's host has no function {fn['name']} (this copy splits or names this part of the "
                    "game differently), so the room can't judge or keep a match for it. Work on another function "
                    "(claim_function), or ask the host to split or name it the same way.")}
            if exact:
                return {"matched": False, "score": None,
                        "text": f"⚠️ This copy scored 0, but the room's host couldn't be reached to confirm it ({e}). "
                                "NOT saved as a match: compile it again once the host is back."}
            return None  # unreachable: judged here as usual (this copy's score, nothing accepted on it alone)
        size = got.get("size")
        if got.get("matched") and size is not None and size != fn["size"]:
            return {"matched": False, "score": None, "text": _host_cut_differs(fn, size)}
        if got.get("matched"):
            return {"matched": True, "score": 0, "text": ""}
        host = got.get("score")
        if exact:
            return {"matched": False, "score": host, "text": _host_refused(host)}
        return {"matched": False, "score": host,
                "text": (f"score {host} on the room's host" if host is not None else "❌ doesn't compile on the room's host")
                        + (" (this copy can't build this source's decomp-* compiler comments: the host's score is "
                           "the one that counts)" if failed_here else
                           " (this source has decomp-* compiler comments only the host defines, so this copy's "
                           "score doesn't count; the host's does)") + ". Keep iterating."}

    def _unknown_directives(self, fn, source: str) -> bool:
        """Whether `source` has decomp-* comments this copy can't apply (only a host that defines them can judge it)."""
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        return bool(self._directives(proj, source, fn).unknown)

    def reopen_unconfirmed(self, slug: str, host_matched: list[dict]) -> list[str]:
        """Member side, first sync with the room: the host's full list of matches arrives, and a match here that
        isn't on it was never confirmed by the judge that counts. That covers this copy's own agents' matches
        (an older copy accepted them locally) and matches credited to the room that the room doesn't have (an
        older copy applied a host match to the wrong overlay at the same address). Reopen them, their source
        kept as the best so far. Compared by name; by address only where neither side has two functions there
        (overlays reuse addresses, and names for them can differ between copies). Returns their names."""
        names = {m["name"] for m in host_matched if m.get("name")}
        host_size = {m["name"]: m["size"] for m in host_matched if m.get("name") and m.get("size") is not None}
        host_at: dict[int, int] = {}
        for m in host_matched:
            host_at[m["vram"]] = host_at.get(m["vram"], 0) + 1
        proj = self._project(slug)
        here = {r["vram"]: r["n"] for r in self.store.all(
            "SELECT vram, COUNT(*) AS n FROM functions WHERE project_id=? GROUP BY vram", proj["id"])}
        rows = self.store.all("SELECT f.*, EXISTS(SELECT 1 FROM verification_receipts v WHERE v.function_id=f.id) AS "
                              "verified FROM functions f WHERE f.project_id=? AND f.status='matched'", proj["id"])
        # ...and a host match taken by name without being verified here, whose function the host cuts to another
        # size (it merged a false split): this copy's function is other code, so the match isn't this copy's
        rows = [r for r in rows if (r["name"] not in names and not (host_at.get(r["vram"]) == 1 and here.get(r["vram"]) == 1))
                or (r["name"] in names and not r["verified"] and self._other_cut(r, proj, host_size.get(r["name"])))]
        if not rows:
            return []
        with self.store.tx() as db:
            for r in rows:
                db.execute("DELETE FROM verification_receipts WHERE function_id=?", (r["id"],))
                meta = json.loads(r["meta"] or "{}")
                meta.pop("host_only", None)
                db.execute("UPDATE functions SET status='open', best_score=NULL, best_source=matched_source, "
                           "best_agent_id=matched_agent_id, matched_source=NULL, matched_agent_id=NULL, "
                           "matched_at=NULL, meta=?, revision=revision+1 WHERE id=?",
                           (json.dumps(meta) if meta else None, r["id"]))
            db.execute("UPDATE projects SET matched_epoch=matched_epoch+1 WHERE id=?", (proj["id"],))
            self._event(db, proj["id"], None, "collab",
                        f"reopened {len(rows)} match{'es' if len(rows) != 1 else ''} the room's host doesn't have, or "
                        f"cuts to another size "
                        f"(their code is kept as the best so far): " + ", ".join(r["name"] for r in rows[:10])
                        + (", ..." if len(rows) > 10 else ""))
        return [r["name"] for r in rows]

    def _other_cut(self, fn, proj, host_size: int | None, source: str | None = None) -> int | None:
        """The size of the host's function when it isn't this copy's `fn` (the host cut that code differently: it
        merged a false split, say), else None. From the host's word (`host_size`) when it sends it; otherwise
        measured: the host's source (`source`, default fn's matched source), built here with every decomp-*
        comment it has, comes out another length. Measured once per cut: remembered in meta.cut_checked."""
        if host_size is not None:
            return host_size if host_size != fn["size"] else None
        source = source if source is not None else fn["matched_source"]
        meta = json.loads(fn["meta"] or "{}")
        if not source or meta.get("cut_checked") == [fn["size"], hashlib.sha256(source.encode()).hexdigest()[:16]]:
            return None
        if self._directives(proj, source, fn).unknown:
            return None  # built here without some of its comments: its length says nothing
        try:
            result, cand, _, _, _ = self._compile_and_evaluate(fn, proj, source, self._context(proj["id"], fn["unit"]))
        except Exception:
            return None
        built = len(getattr(cand, "code", None) or b"") if result is not None and result.ok and cand is not None else 0
        if not built:
            return None
        if built != fn["size"]:
            return built
        meta["cut_checked"] = [fn["size"], hashlib.sha256(source.encode()).hexdigest()[:16]]
        with self.store.tx() as db:
            db.execute("UPDATE functions SET meta=? WHERE id=?", (json.dumps(meta), fn["id"]))
        return None

    @staticmethod
    def _report(link, agent, name, source, score) -> None:
        try:
            link.call("/collab/best", agent, function=name, source=source, score=score)
        except NetworkError:
            pass

    def apply_reported_best(self, slug: str, name: str, score: int, source: str, agent: Agent) -> None:
        """Host side: a room member reports a better attempt. Their score is a hint, never authority (plan
        X-01): the source is recompiled and scored locally, and only the locally measured result is
        recorded — as the function's best attempt, or as its match when it really matches."""
        fn = self._function(name, slug)
        if fn["status"] != "open" or score <= 0 or (fn["best_score"] is not None and score >= fn["best_score"]):
            return
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        ctx = self._context(fn["project_id"], fn["unit"])
        snap = snapshots.capture(fn, proj, ctx)
        result, cand, _, _, d = self._compile_and_evaluate(fn, proj, source, ctx)
        matched_now = False
        local = None
        if result.ok and cand is not None:
            if self._hash_only(fn):
                matched_now = cand.fingerprint() == fn["fingerprint"]
                local = 0 if matched_now else None
            else:
                matched_now = d.bytes_match
                local = d.score
        ok, why = snapshots.check_current(self, proj, fn, snap)
        if not ok:
            self._record(fn, agent, local, source, ctx["version"], compiled=result.ok and cand is not None,
                         via="room", snapshot=snap, stale_reason=why)
        elif matched_now:
            self._record(fn, agent, 0, source, ctx["version"], matched=True, compiled=True, via="room", snapshot=snap,
                         object_digest=cand.fingerprint())
        elif local is not None and (fn["best_score"] is None or local < fn["best_score"]):
            self._record(fn, agent, local, source, ctx["version"], compiled=True, via="room", snapshot=snap)
        else:
            self._record(fn, agent, local, source, ctx["version"], compiled=result.ok and cand is not None,
                         via="room", snapshot=snap, stale_reason="room score not confirmed locally")

    def release(self, agent: Agent, function: str, notes: str, project: str | None = None) -> str:
        if (fork := self.fork_slug(project)):
            return self.forks.release(agent, fork, function, notes)
        link = self._link(project)
        if link is None:
            out = super().release(agent, function, notes, project)
            self._noted(project, function, notes, agent)
            return out
        # always tell the host, even when this copy has no claim: the host's claim is the one that blocks
        name = self._function(function, project)["name"]
        try:
            link.call("/collab/release", agent, function=name, notes=notes)
            on_host = True
        except NetworkError as e:
            if "don't hold a claim" not in str(e):
                raise
            on_host = False
        try:
            return super().release(agent, function, notes, project)
        except NetworkError:
            if not on_host:
                raise
            return f"Released {name} in the room (this copy had no claim on it). Call claim_function for a new one."

    def edit_context(self, agent: Agent, project: str | None, old: str, new: str, message: str,
                     function: str | None = None) -> str:
        self._rom_only(project, "edit_context")
        link = self._link(project)
        if link is None:
            return super().edit_context(agent, project, old, new, message, function)
        text = link.call("/collab/context", agent, old=old, new=new, message=message, function=function)
        if text.startswith("✅"):
            link.sync()
        return text

    def add_note(self, agent: Agent, project: str | None, text: str, function: str | None = None) -> str:
        link = self._link(project)
        if link is None:
            out = super().add_note(agent, project, text, function)
            if function:
                self._noted(project, function, text, agent)
            return out
        name = self._function(function, project)["name"] if function else None
        return link.call("/collab/note", agent, text=text, function=name) or "Noted, and shared with the room."

    def _noted(self, project: str | None, function: str, text: str, agent: Agent) -> None:
        """Tell the auto-fixer about a note on this copy's own project (after it was saved; never in its way)."""
        if not (text or "").strip():
            return
        try:
            proj = self._project(project)
            if self.is_rom(proj) and not self.fork_slug(proj["slug"]):
                self.on_note(proj["slug"], self._function(function, proj["slug"])["name"], text, agent)
        except Exception as e:  # the note is saved either way
            logging.getLogger("decomp.wtf").info("auto-fixer on a note: %s", e)

    def is_fixer(self, agent: Agent) -> bool:
        return agent.id in self.fixer_ids

    def require_fixer(self, agent: Agent) -> None:
        if not self.is_fixer(agent):
            raise NetworkError("only the auto-fixer can do this (decomp.wtf starts it when notes ask for it)")

    def fix_boundary(self, agent: Agent, project: str | None, first: str, second: str, at: str, reason: str) -> str:
        from . import boundary
        self.require_fixer(agent)
        slug = self._project(project)["slug"]
        with self.store.lock:
            pid = self._project(slug)["id"]
            self._require_not_rescanning(pid)
            self._active_attempts[pid] = self._active_attempts.get(pid, 0) + 1
        try:
            return boundary.fix_boundary(self, agent, slug, first, second, at, reason)
        finally:
            with self.store.lock:
                self._active_attempts[pid] -= 1

    def set_aside_function(self, agent: Agent, project: str | None, function: str, reason: str) -> str:
        from . import boundary
        self.require_fixer(agent)
        return boundary.set_aside(self, agent, self._project(project)["slug"], function, reason)

    def queue_for_permuter(self, agent: Agent, project: str | None, function: str, reason: str = "") -> str:
        from . import boundary
        self.require_fixer(agent)
        return boundary.queue_permuter(self, agent, self._project(project)["slug"], function, reason)

    def research_notes(self, slug: str, function: str) -> str:
        """The researchers' notes on a function (newest last), without their [research] mark; "" if none."""
        fn = self._function(function, slug)
        rows = self.store.all("SELECT text FROM notes WHERE function_id=? AND text LIKE ? ORDER BY id",
                              fn["id"], RESEARCH_PREFIX + "%")
        return "\n\n".join(r["text"].removeprefix(RESEARCH_PREFIX).strip() for r in rows)

    def add_research_note(self, agent: Agent, project: str | None, function: str, text: str) -> str:
        fn = self._function(function, project)
        with self._research_lock:
            self._research_held.pop(fn["id"], None)
            if self.research_pin.get(agent.id) == fn["name"]:  # its one function is done
                self.research_pin.pop(agent.id, None)
        link = self._link(project)
        if link is None:
            return super().add_research_note(agent, project, function, text)
        out = link.call("/collab/note", agent, text=research_note_text(text), function=fn["name"])
        with self.store.tx() as db:  # the researcher's progress, kept here (the note itself lives in the room)
            self._event(db, fn["project_id"], agent.id, "research", fn["name"])
        return out or f"Saved, and shared with the room. Next: next_to_research() for another function."

    def research_candidates(self, slug: str, agent_id: int = 0, count: int = 1, reserve: bool = False) -> list:
        """Open functions nobody holds and nobody has researched, in the order matchers will claim them (so research
        lands just ahead of them), leaving out those another researcher was handed in the last RESEARCH_HOLD
        seconds. `reserve`: hand these to agent_id for that long. Read-only otherwise: nothing is claimed."""
        proj = self.project_row(slug)
        if (pinned := self.research_pin.get(agent_id)):  # a person asked for this one function: only it
            row = self.store.one("SELECT * FROM functions WHERE project_id=? AND name=? AND status!='matched'",
                                 proj["id"], pinned)
            if row is not None and reserve:
                with self._research_lock:
                    self._research_held[row["id"]] = (agent_id, time.time() + RESEARCH_HOLD)
            return [row] if row is not None else []
        order = STRATEGIES.get("finish" if self.fork_slug(slug) else "easiest") or "f.n_insns ASC"
        now = time.time()
        with self._research_lock:
            for fid, (_, until) in list(self._research_held.items()):
                if until <= now:
                    del self._research_held[fid]
            others = {fid for fid, (aid, _) in self._research_held.items() if aid != agent_id}
            rows = self.store.all(
                f"""SELECT f.* FROM functions f WHERE f.project_id=? AND f.status='open'
                    AND f.id NOT IN (SELECT function_id FROM claims)
                    AND f.id NOT IN (SELECT function_id FROM notes WHERE function_id IS NOT NULL AND text LIKE ?)
                    AND f.name NOT IN (SELECT detail FROM events WHERE project_id=? AND kind='research' AND created_at > ?)
                    ORDER BY f.name IN (SELECT detail FROM events WHERE project_id=? AND kind='release'
                                        AND created_at > ?), {order} LIMIT ?""",
                # (a research event too: in a room the note lives on the host until it syncs back)
                proj["id"], RESEARCH_PREFIX + "%", proj["id"], now - RESEARCH_HOLD, proj["id"], now - RELEASE_COOLDOWN,
                count + len(others))
            rows = [r for r in rows if r["id"] not in others][:count]
            if reserve and agent_id:
                for r in rows:
                    self._research_held[r["id"]] = (agent_id, now + RESEARCH_HOLD)
        return rows

    def release_research(self, agent_id: int) -> None:
        """A researcher stopped: what it was handed goes back to the others."""
        with self._research_lock:
            for fid, (aid, _) in list(self._research_held.items()):
                if aid == agent_id:
                    del self._research_held[fid]

    def disconnect_agent(self, name: str) -> list[str]:
        """The user cut off an outside agent (one connected over MCP, or a helper the app no longer runs): what it
        claimed goes back to the others, and its token stops working with a message that says why (see
        Network.authenticate). Returns the functions it gave back."""
        row = self.store.one("SELECT id, name, owner, trusted, origin, token_hash FROM agents WHERE name=?", name)
        if row is None:
            raise NetworkError(f"no agent named {name!r}")
        if row["origin"] not in ("chat", "local") or name in ("you", "function-nuke", "autopilot"):
            raise NetworkError(f"{name} isn't an agent connected to this computer")
        agent = Agent(row["id"], row["name"], row["owner"], bool(row["trusted"]))
        freed = []
        for c in self.store.all("SELECT f.name, p.slug FROM claims c JOIN functions f ON f.id=c.function_id "
                                "JOIN projects p ON p.id=f.project_id WHERE c.agent_id=?", agent.id):
            try:
                if self.fork_slug(c["slug"]) or self._link(c["slug"]) is not None:
                    self.release(agent, c["name"], "", c["slug"])  # the room's host (or the fork) is told too
                else:  # not the function's fault: no abandon counted against it
                    with self.store.tx() as db:
                        fn = self._function(c["name"], c["slug"])
                        db.execute("DELETE FROM claims WHERE function_id=? AND agent_id=?", (fn["id"], agent.id))
                        self._event(db, fn["project_id"], agent.id, "release", fn["name"])
                freed.append(c["name"])
            except NetworkError as e:
                logging.getLogger("decomp.wtf").info("disconnecting %s: releasing %s: %s", name, c["name"], e)
        with self.store.tx() as db:
            db.execute("DELETE FROM claims WHERE agent_id=?", (agent.id,))  # anything the loop couldn't give back
            if not row["token_hash"].startswith(DISCONNECTED):
                db.execute("UPDATE agents SET token_hash=? WHERE id=?", (DISCONNECTED + row["token_hash"], agent.id))
        self.release_research(agent.id)
        self.role.pop(agent.id, None)
        self.fixer_ids.discard(agent.id)
        return freed

    def next_to_research(self, agent: Agent, slug: str, count: int = 1) -> str:
        if self.role.get(agent.id) != "researcher":
            raise NetworkError("next_to_research is for researcher helpers; matching agents use claim_function")
        rows = self.research_candidates(slug, agent.id, max(1, min(int(count or 1), 3)), reserve=True)
        if not rows:
            return ("Nothing left to research here: every open function is claimed or has a research note already. "
                    "Stop now.")
        lines = [f"Yours to research for the next {RESEARCH_HOLD // 60} minutes (other researchers skip them; nothing "
                 "is claimed, so never call claim_function or release_function):"]
        for r in rows:
            where = f" at 0x{r['vram']:08X}" if r["vram"] else (f" in {r['unit']}" if r["unit"] else "")
            best = "nothing compiled yet" if r["best_score"] is None else f"best score {r['best_score']}"
            lines.append(f"- {r['name']}{where}: {r['n_insns']} instructions, {best}, {r['attempts']} attempts")
        lines.append("For each: get_function(name), find_similar(name) and search_notes, then add_research_note(name, "
                     "text).")
        return "\n".join(lines)

    def _remote_agent(self, name: str) -> int:
        label = "@" + name
        if label in self._remote_ids:
            return self._remote_ids[label]
        row = self.store.one("SELECT id FROM agents WHERE name=?", label)
        if row is None:
            with self.store.tx() as db:
                aid = db.execute("INSERT INTO agents(name,owner,token_hash,created_at,trusted,origin) VALUES(?,?,?,?,1,?)",
                                 (label, "room", "remote:" + secrets.token_hex(16), time.time(), "remote")).lastrowid
        else:
            aid = row["id"]
        self._remote_ids[label] = aid
        return aid

    def remote_event(self, project_id: int, kind: str, detail: str, agent_name: str, created_at: float) -> None:
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            db.execute("INSERT INTO events(project_id,agent_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                       (project_id, aid, kind, detail, created_at))

    def apply_remote_match(self, slug: str, vram: int, source: str, agent_name: str, name: str | None = None,
                           size: int | None = None) -> None:
        """Member side: the room's host reports this function matched. A remote report is never accepted on
        its own (plan X-01): the reported source is recompiled against the CURRENT local context and
        evaluated against the local target; only a real local match goes through the normal acceptance
        path (_record: receipt, matched_epoch). When this copy can't reproduce it (the host compiled with
        decomp-* comments or a compiler this copy lacks) the attempt is kept as historical evidence, and in a
        linked room, where the host is the judge, the function is still marked matched as the host's (no
        receipt: nothing was verified here) - unless the host's function has another size (`size`, or the length
        the source builds to here when it has no comments this copy can't apply): then the two copies cut this
        code differently and the host's match is of other code. Found by name (overlays reuse
        addresses), else by a unique address."""
        proj = self._project(slug)
        fn = self.store.one("SELECT * FROM functions WHERE project_id=? AND name=?", proj["id"], name) if name else None
        if fn is None:
            same = self.store.all("SELECT * FROM functions WHERE project_id=? AND vram=?", proj["id"], vram)
            if len(same) != 1:  # none, or several overlays/banks at that address: never guess which one matched
                return
            fn = same[0]
        if fn["status"] == "matched":
            return
        ctx = self._context(proj["id"], fn["unit"])
        if ctx is None:
            return
        agent = Agent(self._remote_agent(agent_name), "@" + agent_name, "room", True)
        snap = snapshots.capture(fn, proj, ctx)
        try:
            result, cand, _, _, d = self._compile_and_evaluate(fn, proj, source, ctx)
        except Exception:  # a compiler this copy lacks: not reproducible here
            result, cand, d = None, None, None
        compiled = bool(result is not None and result.ok and cand is not None)
        matched_now = False
        score = None
        if compiled:
            if self._hash_only(fn):
                matched_now = cand.fingerprint() == fn["fingerprint"]
                score = 0 if matched_now else None
            else:
                matched_now = d.bytes_match
                score = d.score
        ok, why = snapshots.check_current(self, proj, fn, snap)
        if matched_now and ok and self._record(fn, agent, score, source, ctx["version"], matched=True, compiled=True,
                                               via="room", snapshot=snap, object_digest=cand.fingerprint()) is None:
            return
        built = len(getattr(cand, "code", None) or b"") if compiled else 0
        if size is None and built and built != fn["size"] and not self._directives(proj, source, fn).unknown:
            size = built  # built here the host's way, it is another length: the host's cut is another
        other_cut = size is not None and size != fn["size"]
        self._record(fn, agent, score, source, ctx["version"], matched=False, compiled=compiled, via="room",
                     snapshot=snap, stale_reason=why or (f"the host's function is {size} bytes, this copy's {fn['size']}"
                                                         if other_cut else "remote match failed local verification"))
        if self.cfg(proj).get("link") and not other_cut:
            self._mark_host_match(fn["id"], agent, source)

    HOST_ONLY = ("matched on the room's host, which judges the room; this copy can't rebuild it (decomp-* compiler "
                 "comments or a compiler it lacks), so it carries no local receipt")

    def _mark_host_match(self, function_id: int, agent: Agent, source: str) -> None:
        """A room's match this copy can't reproduce: matched, credited to the host's agent, its source kept, and
        meta.host_only saying why there is no verification receipt."""
        with self.store.tx() as db:
            row = db.execute("SELECT project_id, status, meta FROM functions WHERE id=?", (function_id,)).fetchone()
            if row is None or row["status"] == "matched":
                return
            meta = json.loads(row["meta"] or "{}")
            meta["host_only"] = self.HOST_ONLY
            db.execute("UPDATE functions SET status='matched', matched_source=?, matched_agent_id=?, matched_at=?, "
                       "best_score=0, best_source=?, meta=? WHERE id=?",
                       (source, agent.id, time.time(), source, json.dumps(meta), function_id))
            db.execute("DELETE FROM claims WHERE function_id=?", (function_id,))
            db.execute("DELETE FROM search_budgets WHERE function_id=?", (function_id,))
            db.execute("UPDATE projects SET matched_epoch=matched_epoch+1 WHERE id=?", (row["project_id"],))

    def apply_remote_match_named(self, slug: str, name: str, agent_name: str) -> None:
        """A git project's room: who matched a function (the code itself arrives with the host's commits)."""
        proj = self._project(slug)
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            row = db.execute("SELECT id, status, matched_agent_id FROM functions WHERE project_id=? AND name=?",
                             (proj["id"], name)).fetchone()
            if row is None:
                return
            if row["status"] != "matched":
                db.execute("UPDATE functions SET status='matched', matched_agent_id=?, matched_at=? WHERE id=?",
                           (aid, time.time(), row["id"]))
            elif row["matched_agent_id"] is None:
                db.execute("UPDATE functions SET matched_agent_id=? WHERE id=?", (aid, row["id"]))
            db.execute("DELETE FROM claims WHERE function_id=?", (row["id"],))

    def apply_remote_note_named(self, slug: str, function: str | None, text: str, agent_name: str, created_at: float) -> None:
        proj = self._project(slug)
        row = self.store.one("SELECT id FROM functions WHERE project_id=? AND name=?", proj["id"], function) if function else None
        fid = row["id"] if row else None
        if self.store.one("SELECT 1 FROM notes WHERE project_id=? AND text=? AND function_id IS ?", proj["id"], text, fid):
            return
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                       (proj["id"], fid, aid, text, created_at))

    def apply_remote_best(self, slug: str, vram: int, score: int, source: str) -> None:
        proj = self._project(slug)
        with self.store.tx() as db:
            db.execute("UPDATE functions SET best_score=?, best_source=? WHERE project_id=? AND vram=? AND status='open' "
                       "AND (best_score IS NULL OR best_score > ?)", (score, source, proj["id"], vram, score))

    def apply_remote_note(self, slug: str, host_id: int, vram: int | None, text: str, agent_name: str, created_at: float) -> None:
        proj = self._project(slug)
        fid = None
        if vram is not None:
            row = self.store.one("SELECT id FROM functions WHERE project_id=? AND vram=?", proj["id"], vram)
            fid = row["id"] if row else None
        if self.store.one("SELECT 1 FROM notes WHERE project_id=? AND text=? AND function_id IS ?", proj["id"], text, fid):
            return
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                       (proj["id"], fid, aid, text, created_at))

    def apply_remote_context(self, slug: str, header: str, host_version: int) -> None:
        proj = self._project(slug)
        if self._context(proj["id"])["header"] == header:
            return
        with self.store.tx() as db:
            version = self._next_version(db, proj["id"])
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (proj["id"], version, header, None, f"from the room (host v{host_version})",
                                                  time.time(), ""))

    def apply_remote_layout(self, slug: str, functions: list[dict], symbols: list[dict]) -> None:
        """Mirror the room's merges, splits and names. The host already regression-tested them on the same ROM."""
        proj = self._project(slug)
        pid = proj["id"]
        if functions:  # the host joined functions or moved a cut: join here first, the splits below cut again
            from . import boundary
            boundary.follow_layout(self, proj, functions)
        local = {r["vram"]: r for r in self.store.all("SELECT * FROM functions WHERE project_id=?", pid)}
        for f in sorted(functions, key=lambda f: f["vram"]):
            if f["vram"] in local:
                continue
            owner = next((r for r in local.values() if r["vram"] < f["vram"] < r["vram"] + r["size"]), None)
            if owner is None or owner["status"] == "matched":
                continue
            rf = self._parse_asm(owner["name"], owner["asm"], owner["unit"])
            try:
                head, tail = split.split_function(rf, f["vram"], f["name"])
            except ValueError:
                continue
            syms = self.symbols(pid)
            syms.add(Symbol(f["name"], f["vram"], tail.size, "func"))
            th, tt = self._make_target(proj, head, syms), self._make_target(proj, tail, syms)
            with self.store.tx() as db:
                db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL WHERE id=?",
                           (head.size, len(head.lines), th.code, json.dumps(th.to_json()), self._asm_source(head), owner["id"]))
                db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (pid, f["name"], tail.size, len(tail.lines), tt.code,
                                                            json.dumps(tt.to_json()), self._asm_source(tail), None,
                                                            owner["unit"], f["vram"], tail.rom))
                db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                           (pid, f["name"], f["vram"], tail.size, "func"))
            local = {r["vram"]: r for r in self.store.all("SELECT * FROM functions WHERE project_id=?", pid)}
        self._xrefs.pop(pid, None)
        if functions:  # a cut the host moved earlier: the piece the splits just cut out takes in what follows
            from . import boundary
            boundary.follow_layout(self, proj, functions)
        syms = self.symbols(pid)
        for sym in symbols:
            current, off = syms.name_at(sym["address"])
            if off == 0 and current == sym["name"]:
                continue
            old = current if off == 0 else f"D_{sym['address']:08X}"
            word = re.compile(rf"\b{re.escape(old)}\b")
            with self.store.tx() as db:
                db.execute("DELETE FROM symbols WHERE project_id=? AND (name=? OR name=?)", (pid, old, sym["name"]))
                db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                           (pid, sym["name"], sym["address"], sym["size"], sym["kind"]))
                at = db.execute("SELECT COUNT(*) FROM functions WHERE project_id=? AND vram=?", (pid, sym["address"])).fetchone()[0]
                if at == 1:  # several overlays at this address: which one the host means is unknown, rename none
                    db.execute("UPDATE functions SET name=? WHERE project_id=? AND vram=?", (sym["name"], pid, sym["address"]))
                for r in db.execute("SELECT id, matched_source, best_source FROM functions WHERE project_id=? AND "
                                    "(matched_source IS NOT NULL OR best_source IS NOT NULL)", (pid,)).fetchall():
                    db.execute("UPDATE functions SET matched_source=?, best_source=? WHERE id=?",
                               (word.sub(sym["name"], r["matched_source"]) if r["matched_source"] else None,
                                word.sub(sym["name"], r["best_source"]) if r["best_source"] else None, r["id"]))
            self._syms.pop(pid, None)
            syms = self.symbols(pid)

    # ------------------------------------------------------------------ export

    def export_rom(self, slug: str, log=print) -> dict:
        """Write a rebuildable source tree (the base for ports and mods) and prove it rebuilds the game."""
        if self.fork_slug(slug):  # a fork's work is already in its source tree, committed
            return {"path": self._project(slug)["root"], "identical": None}
        from ..rom.export import export_rom
        from ..rom.export_gba import export_gba

        proj = self._project(slug)
        export = export_gba if self.cfg(proj).get("platform") == "gba" else export_rom
        res = export(self, slug, Path(proj["root"]) / "source", log=log)
        with self.store.tx() as db:  # shown on the project page: what's in the tree and whether it rebuilds
            cfg = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"])
            cfg["export"] = {"t": time.time(), "path": str(res["path"]), "identical": res.get("identical"),
                             "matched": res.get("matched"), "functions": res.get("functions"),
                             "fallback": len(res.get("fallback") or [])}
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
        return res

    # ------------------------------------------------------------------ for the GUI

    def overview(self, slug: str) -> dict:
        proj = self._project(slug)
        self._set_aside_once(proj)
        last = self.store.one("SELECT MAX(created_at) AS t FROM events WHERE project_id=?", proj["id"])["t"]
        cfg = self.cfg(proj)
        # matches by the helper agents the app runs (their spend is what `spend` counts)
        helpers = self.store.one(
            "SELECT COUNT(*) AS n FROM functions f JOIN agents a ON a.id=f.matched_agent_id WHERE f.project_id=? "
            "AND f.status='matched' AND a.origin='local' AND a.name NOT IN ('you','autopilot')", proj["id"])["n"]
        twins = self.twins.jobs.get(proj["slug"])
        return self._overview(slug, proj) | {"last_activity": last, "autopilot": self.autopilot_status(slug),
                                             "permuter": self.permuter.status(proj["slug"]),
                                             "twins": None if twins is None else {
                                                 k: getattr(twins, k) for k in ("state", "pairs", "tried", "matched", "asm", "error")},
                                             "helper_matches": helpers,
                                             "spend": cfg.get("spend"), "instructions": cfg.get("instructions") or "",
                                             "priority": self._open_priority(proj, cfg.get("priority", []))}

    def _set_aside_once(self, proj) -> None:
        """BIOS calls and hand-written assembly out of agents' way when a ROM project is first opened, not only when
        agents start (projects imported before a detector existed get it too). Matched functions are never touched."""
        if proj["id"] in self._set_aside_checked or self.fork_slug(proj["slug"]):
            return
        self._set_aside_checked.add(proj["id"])
        try:
            self.twins.set_aside_asm(proj)
        except Exception as e:  # the overview must open regardless
            logging.getLogger("decomp.wtf").info("set aside on %s: %s", proj["slug"], e)

    @staticmethod
    def set_aside_info(fn) -> dict:
        """Why a function is out of agents' way (its page shows it), and whether a person gave it back to them."""
        meta = json.loads(fn["meta"] or "{}") if fn["meta"] else {}
        return {"bios": meta.get("bios"), "asm_reason": meta.get("asm_code") or (
                    f"the same code as {meta['asm_twin']}, which is assembly" if meta.get("asm_twin") else None),
                "for_agents": bool(meta.get("for_agents")), "needs_symbols": linkblock.summary(meta)}

    def give_to_agents(self, slug: str, name: str, give: bool = True) -> dict:
        """A person's say over a function set aside as a BIOS call or hand-written assembly. give: agents get it
        anyway, and the detectors leave it alone from then on; not give: it's set aside again. Never touches a
        matched function."""
        if slug in self.links:
            raise NetworkError("in a team room the host decides which functions agents get")
        fn = self._function(name, slug)
        meta = json.loads(fn["meta"] or "{}") if fn["meta"] else {}
        with self.store.tx() as db:
            if give:
                if fn["status"] != "asm":
                    raise NetworkError(f"{fn['name']} isn't set aside")
                meta["for_agents"] = True
                db.execute("UPDATE functions SET status='open', meta=? WHERE id=? AND status='asm'",
                           (json.dumps(meta), fn["id"]))
            else:
                if fn["status"] != "open" or not meta.get("for_agents"):
                    raise NetworkError("only a function given to agents by hand can be set aside again")
                if db.execute("SELECT 1 FROM claims WHERE function_id=?", (fn["id"],)).fetchone():
                    raise NetworkError(f"{fn['name']} is claimed right now: release it first")
                meta.pop("for_agents", None)
                db.execute("UPDATE functions SET status='asm', meta=? WHERE id=? AND status='open'",
                           (json.dumps(meta), fn["id"]))
            self._event(db, fn["project_id"], None, "set_aside",
                        f"{fn['name']} {'given to agents by hand' if give else 'set aside again by hand'}")
        self.twins.forget(self._project(slug))
        return {"name": fn["name"], "status": "open" if give else "asm"} | self.set_aside_info(
            self.store.one("SELECT meta FROM functions WHERE id=?", fn["id"]))

    def _open_priority(self, proj, names: list[str]) -> list[str]:
        """The ★ Next in line functions still to do (a match takes one off the list; this hides any done another way)."""
        if not names:
            return []
        done = {r["name"] for r in self.store.all(
            f"SELECT name FROM functions WHERE project_id=? AND status='matched' AND name IN ({','.join('?' * len(names))})",
            proj["id"], *names)}
        return [n for n in names if n not in done]

    def _overview(self, slug: str, proj) -> dict:
        if (fork := self.fork_slug(slug)):
            return self.forks.overview(fork)
        cfg = self.cfg(proj)
        agg = self.store.one(
            "SELECT COUNT(*) AS total, SUM(status='matched') AS matched, SUM(size) AS bytes, "
            "SUM(CASE WHEN status='matched' THEN size ELSE 0 END) AS matched_bytes, SUM(attempts) AS attempts "
            "FROM functions WHERE project_id=?", proj["id"])
        tc = toolchains.get(cfg["compiler"]["toolchain"])
        return {
            "slug": proj["slug"], "title": proj["name"], "description": proj["description"],
            "platform": cfg.get("platform", "n64"), "platform_name": detect.PLATFORMS.get(cfg.get("platform", "n64"), ""),
            "compilers": PLATFORM_COMPILERS.get(cfg.get("platform", "n64"), []),
            "rom": {k: cfg["rom"].get(k) for k in ("title", "game_code", "region", "size", "sha1", "byte_order", "compiler_guess")}
            | {"path": str(Path(proj["root"]) / name)
               if proj["root"] and (name := cfg["rom"].get("program") or cfg["rom"].get("file")) else None},
            "compiler": {"toolchain": tc.id, "name": SHORT_NAMES.get(tc.id, tc.name), "flags": cfg["compiler"]["flags"],
                         "installed": toolchains.installed(tc.id) is not None, "proprietary": tc.proprietary,
                         "supported": tc.supported, "linux": tc.via_linux,
                         "why": None if tc.supported else toolchains.unsupported_cpu()},
            "compiler_profiles": cfg.get("compiler_profiles", {}), "compiler_ranges": cfg.get("compiler_ranges", []),
            "compiler_functions": cfg.get("compiler_functions", {}),
            "functions": agg["total"] or 0, "matched": agg["matched"] or 0, "bytes": agg["bytes"] or 0,
            "matched_bytes": agg["matched_bytes"] or 0, "attempts": agg["attempts"] or 0,
            "context_version": self._context(proj["id"])["version"],
            "folder": proj["root"],
            "export": cfg.get("export"),
            "exportable": cfg.get("platform", "n64") in ("n64", "psx", "ps2", "gba"),
            "room": self._room_info(proj["slug"]),
        }

    def _room_info(self, slug: str) -> dict | None:
        if slug in self.links:
            link = self.links[slug]
            return {"role": "guest", "host": link.url, "member": link.member, "error": link.error,
                    "members": link.members, "connected": time.time() - link.last_ok < 15}
        if slug in self.rooms:
            return {"role": "host", "members": self.rooms[slug].members()}
        return None

    def function_map(self, slug: str) -> list[dict]:
        proj = self._project(slug)
        rows = self.store.all(
            "SELECT f.name, f.vram, f.size, f.n_insns, f.status, f.best_score, f.attempts, f.unit, f.matched_at, f.meta, "
            "a.name AS agent, m.name AS matched_by FROM functions f LEFT JOIN claims c ON c.function_id=f.id "
            "LEFT JOIN agents a ON a.id=c.agent_id LEFT JOIN agents m ON m.id=f.matched_agent_id "
            "WHERE f.project_id=? ORDER BY f.vram, f.unit, f.id", proj["id"])
        link = self.links.get(slug)
        # who works on what in the room: by address, or by name in a git project (its functions have no addresses)
        key = (lambda r: r["name"]) if link is not None and link.fork else (lambda r: r["vram"])
        remote = {key(c): "@" + c["agent"] for c in (link.claims if link is not None else [])}
        return [{"name": r["name"], "vram": r["vram"], "size": r["size"], "insns": r["n_insns"], "unit": r["unit"],
                 "status": ("bios" if r["status"] == "asm" and '"bios"' in (r["meta"] or "") else r["status"])
                 if r["status"] in ("matched", "asm") else "working" if (r["agent"] or key(r) in remote) else "open",
                 "best": r["best_score"], "attempts": r["attempts"], "agent": r["agent"] or remote.get(key(r)),
                 "matched_at": r["matched_at"], "matched_by": r["matched_by"]} for r in rows]

    def map_signature(self, slug: str) -> str:
        """What the function map shows, summed up: it changes when a function is added, matched, tried or claimed.
        A few sums over indexed rows, so the GUI can ask "anything new?" every few seconds without fetching tens of
        thousands of functions (renames and splits come with events, which refetch anyway)."""
        proj = self._project(slug)
        f = self.store.one("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS hi, COALESCE(SUM(attempts), 0) AS at, "
                           "COALESCE(SUM(status='matched'), 0) AS m, COALESCE(SUM(best_score), 0) AS bs, "
                           "COALESCE(MAX(matched_at), 0) AS ma, COALESCE(SUM(status='asm'), 0) AS sa, "
                           "COALESCE(SUM(status='asm' AND meta LIKE '%\"bios\"%'), 0) AS sb "
                           "FROM functions WHERE project_id=?", proj["id"])
        c = self.store.one("SELECT COUNT(*) AS n, COALESCE(SUM(c.function_id * 31 + c.agent_id), 0) AS k FROM claims c "
                           "JOIN functions f ON f.id=c.function_id WHERE f.project_id=?", proj["id"])
        link = self.links.get(slug)
        remote = sorted(str(x.get("vram") or x.get("name")) + x.get("agent", "") for x in (link.claims if link else []))
        return (f"{f['n']}.{f['hi']}.{f['at']}.{f['m']}.{f['bs']}.{f['ma']}.{c['n']}.{c['k']}.{hash(tuple(remote)) & 0xFFFFFF}"
                f".{f['sa']}.{f['sb']}")

    # ------------------------------------------------------------------ what agents spend, and steering them

    def _update_cfg(self, slug: str, change) -> dict:
        """Change a project's config in one transaction (change(cfg) edits it in place)."""
        proj = self._project(slug)
        with self.store.tx() as db:
            cfg = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"])
            change(cfg)
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
        return cfg

    def add_spend(self, slug: str, usd: float, tokens: int) -> None:
        if usd or tokens:
            def change(cfg):
                sp = cfg.setdefault("spend", {"usd": 0.0, "tokens": 0})
                sp["usd"] = round(sp.get("usd", 0) + usd, 6)
                sp["tokens"] = sp.get("tokens", 0) + tokens
            self._update_cfg(slug, change)

    def spend(self, slug: str) -> dict | None:
        return self.cfg(self._project(slug)).get("spend")

    def set_instructions(self, slug: str, text: str) -> None:
        """What the user wants agents to know or focus on, given to every agent on this project."""
        self._update_cfg(slug, lambda cfg: cfg.__setitem__("instructions", text.strip()[:4000]))

    def instructions(self, slug: str) -> str:
        return self.cfg(self._project(slug)).get("instructions") or ""

    def set_priority(self, slug: str, name: str, on: bool = True) -> list[str]:
        """Functions the user wants done next: automatic claims take these first."""
        fn = self._function(name, slug)

        def change(cfg):
            items = [n for n in cfg.get("priority", []) if n != fn["name"]]
            cfg["priority"] = ([fn["name"]] + items) if on else items
        return self._update_cfg(slug, change)["priority"]

    def claim_row(self, agent: Agent, project: str | None = None, function: str | None = None, strategy: str = "easiest",
                  skip: list[str] | tuple = ()):
        self._yield_autopilot(function, project)
        with self.store.lock:
            proj = self._project(project) if project or function is None else self.store.one(
                "SELECT * FROM projects WHERE id=?", self._function(function, project)["project_id"])
            self._require_not_rescanning(proj["id"])
            return self._claim_row_available(agent, project, function, strategy, skip)

    def released_since(self, agent_id: int, project_id: int, names: list[str], since: float) -> set[str]:
        """Which of `names` this agent gave back (release_function) since `since`: it found them not worth doing."""
        return {n for n in names if self.store.one(
            "SELECT 1 FROM events WHERE project_id=? AND agent_id=? AND kind='release' AND detail=? AND created_at>=?",
            project_id, agent_id, n, since)}

    def _claim_row_available(self, agent, project, function, strategy, skip=()):
        if project and not self.fork_slug(project):
            proj = self._project(project)
            names = self.cfg(proj).get("priority", [])
            held = self.store.one("SELECT 1 FROM claims WHERE agent_id=?", agent.id)
            only = agent.id in self.starred_only
            if only and function is not None and function not in names:
                raise NetworkError(f"{function} isn't marked ★ Next in line: this helper works only on the starred ones")
            if function is None and agent.id not in self.focus:  # a Focus picks by its own rule, stars included
                # a starred-only helper that released a star (data, not code...) doesn't get it handed back again
                given_up = self.released_since(agent.id, proj["id"], names, self.starred_only[agent.id]) if only else set()
                skipped = set(self._skippable(proj, skip)) if skip else set()  # set aside in a room member's copy
                for name in names:
                    row = self.store.one("SELECT f.id, f.status FROM functions f WHERE f.project_id=? AND f.name=?", proj["id"], name)
                    if row and row["status"] == "open" and name not in given_up and row["id"] not in skipped and not held and not self.store.one(
                            "SELECT 1 FROM claims WHERE function_id=?", row["id"]):
                        function = name
                        break
                if only and function is None and not held:
                    raise NetworkError("no ★ Next in line function is left for you: the rest are done, released by you "
                                       "or being worked on by others. Stop here: this session ends by itself.")
        return Network.claim_row(self, agent, project, function, strategy, skip)

    # ------------------------------------------------------------------ autopilot

    def start_autopilot(self, slug: str, workers: int = 3) -> dict:
        """Try every open function's machine draft (m2c, compilable form) through the judge, smallest first, in the
        background. Whatever already matches is saved before any agent spends a token on it; drafts that compile
        become the function's best attempt, so agents start from them. Agents and autopilot never collide: a
        function is held only while its draft compiles, and claimed ones are skipped."""
        with self.store.lock:
            pid = self._project(slug)["id"]
            self._require_not_rescanning(pid)
            self._active_attempts[pid] = self._active_attempts.get(pid, 0) + 1
        try:
            return self._start_autopilot_available(slug, workers)
        finally:
            with self.store.lock:
                self._active_attempts[pid] -= 1

    def _start_autopilot_available(self, slug: str, workers: int) -> dict:
        state = self.autopilots.get(slug)
        if state and state["state"] == "running":
            return state
        # first the twins (identical code to something matched: every project), then machine drafts where m2c reads
        # the code and the project's headers are fit for them (ROM projects; games started from scratch)
        try:
            if (fork := self.fork_slug(slug)):  # a GameCube, Wii or DS game started from scratch: its own build judges
                ids = self.forks.autopilot_prepare(fork)
                try_one, workers = self.forks.autopilot_one, min(workers, 2)  # each try builds a file: fewer at once
            else:
                if not seeds.available():
                    raise NetworkError("autopilot's drafts need m2c, the draft decompiler: install it in Setup")
                proj = self._project(slug)
                tc = self.cfg(proj)["compiler"]["toolchain"]
                if not seeds.M2C_TARGETS.get(tc):
                    raise NetworkError("m2c can't read this project's code (no draft decompiler for it yet)")
                ids = [r["id"] for r in self.store.all("SELECT id FROM functions WHERE project_id=? AND status='open' "
                                                       "ORDER BY n_insns, vram", proj["id"])]
                try_one = self._autopilot_one
            no_drafts = ""
        except NetworkError as e:
            ids, try_one, no_drafts = [], None, str(e)
        state = {"state": "running", "total": len(ids), "tried": 0, "matched": 0, "near": 0, "twins": 0,
                 "phase": "twins", "no_drafts": no_drafts, "started_at": time.time(), "stop": threading.Event()}
        self.autopilots[slug] = state
        threading.Thread(target=self._autopilot_run, args=(slug, ids, state, workers, try_one),
                         daemon=True, name=f"autopilot-{slug}").start()
        return state

    def stop_autopilot(self, slug: str) -> None:
        if (state := self.autopilots.get(slug)) and state["state"] == "running":
            state["stop"].set()

    def autopilot_status(self, slug: str) -> dict | None:
        state = self.autopilots.get(slug)
        if state:
            return {k: v for k, v in state.items() if k != "stop"}
        last = self.cfg(self._project(slug)).get("autopilot")  # the last run, from before the app restarted
        return {"state": "done", **last} if last else None

    def _autopilot_run(self, slug: str, ids: list[int], state: dict, workers: int, try_one=None) -> None:
        from concurrent.futures import ThreadPoolExecutor

        try:
            agent, _ = self.local_agent("autopilot")
        except NetworkError as e:  # the fleet lockdown refuses the identity: end the run with the reason
            state.update(state="error", error=str(e), finished_at=time.time())
            logging.getLogger("decomp.wtf").warning("autopilot on %s: %s", slug, e)
            return
        proj = self._project(slug)
        try_one = try_one or self._autopilot_one
        try:  # identical code first: free, and it takes those functions off the drafts' list
            if slug not in self.links:  # (a room member's matches come from the host)
                job = self.twins.full_pass(slug)
                state["twins"] = job.matched
                state["matched"] += job.matched
        except Exception as e:
            logging.getLogger("decomp.wtf").info("autopilot twins: %s", e)
        state["phase"] = "drafts"
        ids = [fid for fid in ids if (r := self.store.one("SELECT status FROM functions WHERE id=?", fid)) and r["status"] == "open"]
        state["total"] = len(ids)

        def one(fid: int) -> None:
            if state["stop"].is_set():
                return
            try:
                if try_one == self._autopilot_one and self._autopilot_retrieval(agent, proj, fid):
                    outcome = "match"  # a retrieved candidate was verified (off unless the project enables a route)
                else:
                    outcome = try_one(agent, proj, fid)
            except Exception:  # one odd function must not stop the rest
                outcome = "fail"
            state["tried"] += 1
            if outcome in ("match", "near"):
                state[{"match": "matched", "near": "near"}[outcome]] += 1

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(one, ids))
        finally:
            state["state"] = "stopped" if state["stop"].is_set() else "done"
            state["finished_at"] = time.time()
            with self.store.tx() as db:
                cfg = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"])
                cfg["autopilot"] = {k: state.get(k) for k in ("total", "tried", "matched", "near", "twins", "started_at",
                                                              "finished_at")}
                db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
            drafts = state["matched"] - state.get("twins", 0)
            if drafts:  # (twins announce their own)
                with self.store.tx() as db:
                    self._event(db, proj["id"], agent.id, "autopilot",
                                f"{drafts} of {state['tried']} functions matched from machine drafts")

    def on_match(self, proj, function_id: int, agent: Agent | None = None) -> None:
        """Its twins get its code, at once. (Not for the full twins pass's own matches while it runs: it tries
        those twins itself.) Then the P6.5 acceptance feedback: the similarity index entry is marked
        source-available and the reuse ledger records the retrieval route the verification receipt names
        (A.14). Diagnostics only — never match state. Also takes the function off ★ Next in line: a match
        queues twin matches, or starts a fresh autopilot pass after a manual match when enabled."""
        if proj is None:
            return
        name = self.store.one("SELECT name FROM functions WHERE id=?", function_id)
        if name and name["name"] in self.cfg(self._project(proj["slug"])).get("priority", []):
            self._update_cfg(proj["slug"], lambda cfg: cfg.__setitem__(
                "priority", [n for n in cfg.get("priority", []) if n != name["name"]]))
        if agent is not None and agent.name == "autopilot" and self.twins._full_running(proj["slug"]):
            return
        slug = proj["slug"]
        if (self.autopilot_after_match and agent is not None and agent.name != "autopilot"
                and slug not in self.links):
            with self._autopilot_restart_lock:
                state = self.autopilots.get(slug)
                if not state or state["state"] != "running":
                    self.start_autopilot(slug)
                    return
        self.twins.after_match(proj, function_id)
        try:
            retrieval.feedback.record_acceptance(self, proj, function_id)
        except Exception as e:  # feedback must never break the accepted match
            logging.getLogger("decomp.wtf").info("acceptance feedback: %s", e)

    def try_quietly(self, agent, proj, fn, source: str, via: str = "agent") -> dict:
        """Judge one candidate for a function nobody holds (autopilot's tries): held only while it compiles,
        recorded as an attempt like any other. {"matched", "score"}."""
        now = time.time()
        with self.store.tx() as db:
            self._expire_claims(db)
            if db.execute("SELECT 1 FROM claims WHERE function_id=?", (fn["id"],)).fetchone():
                raise NetworkError(f"{fn['name']} is claimed")
            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + 300))
        try:
            ctx = self._context(proj["id"], fn["unit"])
            snap = snapshots.capture(fn, proj, ctx)
            result = self._compile(proj, ctx["header"], source, fn)
            if not result.ok:
                return {"matched": False, "score": None}
            cand, verdict, _ = self._evaluate(fn, proj, result.obj)
            if cand is None:
                return {"matched": False, "score": None}
            d = scoring.diff(self._target_obj(fn, proj), cand)
            if verdict is not None:
                d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
            judged = self._second_judge(agent, fn, source, d.bytes_match) if d.bytes_match else None
            if judged is not None and not judged["matched"]:  # a room's host has the last word here too
                self._record(fn, agent, judged["score"] or None, source, ctx["version"], compiled=True, snapshot=snap,
                             via=via)
                return {"matched": False, "score": judged["score"]}
            refused = self._record(fn, agent, d.score, source, ctx["version"], matched=d.bytes_match, snapshot=snap,
                                   via=via, object_digest=cand.fingerprint() if d.bytes_match else None)
            return {"matched": d.bytes_match and not refused, "score": d.score}
        finally:
            with self.store.tx() as db:
                db.execute("DELETE FROM claims WHERE function_id=? AND agent_id=?", (fn["id"], agent.id))

    # ------------------------------------------- retrieval candidates into compile-and-verify (P10.2/P10.3)

    def _retrieval_source(self, candidate, registry, proj) -> str:
        """The C a retrieval candidate carries, or "" when none is accessible: the candidate's own source,
        else the file its reference-registry entry permits (a local path, absolute or under the project
        root). Evidence-only candidates and URLs yield nothing: no fabricated or fetched C."""
        own = getattr(candidate, "source", None)
        if isinstance(own, str) and own.strip():
            return own
        location = getattr(candidate, "source_location", "") or ""
        if not location:
            entry_id = getattr(candidate, "entry_id", "") or getattr(getattr(candidate, "sidecar", None), "entry_id", "")
            entry = registry.get(entry_id) if entry_id else None
            location = entry.source_location if entry is not None and entry.usable() else ""
        if not location or "://" in location:
            return ""
        path = Path(location)
        if not path.is_absolute():
            path = Path(proj["root"] or "") / path
        try:
            return path.read_text(errors="replace")[:200_000] if path.is_file() else ""
        except OSError:
            return ""

    def verify_retrieval_candidates(self, agent, proj, fn, limit: int = 2) -> list:
        """Send the top `limit` retrieval candidates that carry accessible source through the same quiet
        judge as try_quietly (held only while compiling, snapshot captured before the compile, recorded
        via='retrieval:<route>'). Retrieval only suggests: the compile-and-diff verdict decides.
        [{"route", "candidate", "matched", "score"}]; [] when the project's retrieval routes are off."""
        config = config_from(proj)
        if not (config.function_id_retrieval or config.bsim_retrieval):
            return []
        registry = self.load_reference_sources(proj["id"])
        out, tried = [], set()
        for hit in self.retrieval_candidates(fn, proj["slug"]):
            if len(out) >= max(0, int(limit)):
                break
            source = self._retrieval_source(hit["candidate"], registry, proj)
            digest = snapshots.sha(source.encode()) if source else ""
            if not source or digest in tried:
                continue
            tried.add(digest)
            result = self.try_quietly(agent, proj, fn, source, via=f"retrieval:{hit['route']}")
            out.append({"route": hit["route"], "candidate": hit["candidate"],
                        "matched": result["matched"], "score": result["score"]})
            if result["matched"]:
                break
        return out

    def _autopilot_retrieval(self, agent, proj, fid: int) -> bool:
        """Autopilot's retrieval step for one function, only when its project enables a retrieval route.
        True when a retrieved candidate matched. Never raises: a failure falls through to the drafts."""
        try:
            proj = self.store.one("SELECT * FROM projects WHERE id=?", proj["id"])
            config = config_from(proj)
            if not (config.function_id_retrieval or config.bsim_retrieval):
                return False
            fn = self.store.one("SELECT * FROM functions WHERE id=?", fid)
            if fn is None or fn["status"] != "open":
                return False
            return any(r["matched"] for r in self.verify_retrieval_candidates(agent, proj, fn))
        except Exception as e:
            logging.getLogger("decomp.wtf").info("autopilot retrieval: %s", e)
            return False

    def _autopilot_one(self, agent, proj, fid: int) -> str:
        """match | near | far | fail | skip, for one function's draft."""
        now = time.time()
        with self.store.tx() as db:
            self._expire_claims(db)
            fn = db.execute("SELECT * FROM functions WHERE id=?", (fid,)).fetchone()
            if fn is None or fn["status"] != "open" or db.execute("SELECT 1 FROM claims WHERE function_id=?", (fid,)).fetchone():
                return "skip"
            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fid, agent.id, now, now + 300))
        try:
            self._seed_for(fn, proj)  # the readable draft, for the agents that come after
            ctx = self._context(proj["id"], fn["unit"])
            snap = snapshots.capture(fn, proj, ctx)
            data_asm ="\n".join(self._jtbl_asm(proj, fn)) if "jtbl_" in (fn["asm"] or "") else None
            res = seeds.draft(self._asm_text(fn, proj), fn["name"], profiles.effective_compiler(self.cfg(proj), fn)["toolchain"],
                              context_header=ctx["header"], data_asm=data_asm, valid=True)
            draft = res.text
            if not draft:
                return "fail"
            source = f"/* machine draft (m2c), tried by autopilot */\n{seeds.M2C_MACROS}\n{draft}\n"
            result = self._compile(proj, ctx["header"], source, fn)
            if not result.ok:
                return "fail"
            cand, verdict, _ = self._evaluate(fn, proj, result.obj)
            if cand is None:
                return "fail"
            d = scoring.diff(self._target_obj(fn, proj), cand)
            if verdict is not None:
                d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
            refused = self._record(fn, agent, d.score, source, ctx["version"], matched=d.bytes_match, snapshot=snap,
                                   object_digest=cand.fingerprint() if d.bytes_match else None)
            return "match" if d.bytes_match and not refused else "near" if d.percent >= 90 else "far"
        finally:
            with self.store.tx() as db:
                db.execute("DELETE FROM claims WHERE function_id=? AND agent_id=?", (fid, agent.id))

    def function_diff(self, slug: str, name: str, which: str = "latest") -> dict:
        """The target next to an attempt, instruction by instruction, for the GUI: the matched C, else the latest
        (or best) attempt, compiled and linked like the judge does. Cached, so watching an agent costs nothing."""
        if (fork := self.fork_slug(slug)):
            return self.forks.function_diff(fork, name)
        fn = self._function(name, slug)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        latest = self.store.one("SELECT source FROM attempts WHERE function_id=? AND source IS NOT NULL "
                                "ORDER BY id DESC LIMIT 1", fn["id"])
        picks = [("matched", fn["matched_source"]), ("best", fn["best_source"]),
                 ("latest", latest["source"] if latest else None)]
        if which == "latest":
            picks = [picks[0], picks[2], picks[1]]
        kind, source = next(((k, v) for k, v in picks if v), (None, None))
        if source is None:
            return {"kind": None, "rows": None}
        ctx = self._context(proj["id"], fn["unit"])
        key = (fn["id"], hashlib.sha1(source.encode()).hexdigest(), ctx["version"], proj["config"])
        if key in self._diffs:
            return self._diffs[key] | {"kind": kind}
        result = self._compile(proj, ctx["header"], source, fn)
        if not result.ok:
            out = {"rows": None, "error": result.log.strip()[-3000:] or "it didn't compile", "source": source}
        else:
            cand, verdict, problems = self._evaluate(fn, proj, result.obj)
            if cand is None:
                out = {"rows": None, "error": f"the object has no function named `{fn['name']}`", "source": source}
            else:
                d = scoring.diff(self._target_obj(fn, proj), cand)
                if verdict is not None:
                    d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
                out = {"rows": [list(r) for r in d.rows], "score": d.score,
                       "percent": 100.0 if d.bytes_match else min(99.9, int(d.percent * 10) / 10),
                       "problems": problems, "source": source}
        if len(self._diffs) > 128:
            self._diffs.pop(next(iter(self._diffs)))
        self._diffs[key] = out
        return out | {"kind": kind}

    def function_detail(self, slug: str, name: str) -> dict:
        if (fork := self.fork_slug(slug)):
            return self.forks.function_detail(fork, name)
        fn = self._function(name, slug)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        attempts = self.store.all(
            "SELECT t.score, t.created_at, a.name AS agent FROM attempts t LEFT JOIN agents a ON a.id=t.agent_id "
            "WHERE t.function_id=? ORDER BY t.id", fn["id"])
        latest = self.store.one("SELECT source, score FROM attempts WHERE function_id=? AND source IS NOT NULL "
                                "ORDER BY id DESC LIMIT 1", fn["id"])
        notes = self.function_notes(fn["id"])
        x = self.xrefs(proj)
        syms = self.symbols(proj["id"])
        if not (fn["matched_source"] or fn["best_source"] or latest) and fn["seed"] is None:
            with self._seed_lock:
                self._seed_for(fn, proj)
            fn = self._function(fn["name"], slug)
        return {
            "name": fn["name"], "vram": fn["vram"], "rom_offset": fn["rom_offset"], "size": fn["size"],
            "insns": fn["n_insns"], "status": fn["status"], "best": fn["best_score"],
            "asm": self._asm_text(fn, proj),
            "source": fn["matched_source"] or fn["best_source"] or (latest["source"] if latest else None) or fn["seed"],
            "source_kind": "matched" if fn["matched_source"] else "best" if fn["best_source"] else
                           "latest" if latest else "draft" if fn["seed"] else None,
            "scores": [{"score": a["score"], "t": a["created_at"], "agent": a["agent"]} for a in attempts],
            "notes": notes,
            "calls": [syms.label(a) for a in sorted(x["calls"].get(fn["vram"], ()))],
            "called_by": [syms.label(a) for a in sorted(x["called_by"].get(fn["vram"], ()))],
        } | self.set_aside_info(fn)

    def stats(self, slug: str) -> dict:
        """Who did what and how fast: the overview's numbers (asked every few seconds, not every second)."""
        pid = self._project(slug)["id"]
        now = time.time()
        rows = self.store.all(
            "SELECT a.name AS agent, COUNT(t.id) AS compiles, MAX(t.created_at) AS last FROM attempts t "
            "JOIN functions f ON f.id=t.function_id JOIN agents a ON a.id=t.agent_id WHERE f.project_id=? GROUP BY a.id", pid)
        matched = {r["agent"]: r["n"] for r in self.store.all(
            "SELECT a.name AS agent, COUNT(*) AS n FROM functions f JOIN agents a ON a.id=f.matched_agent_id "
            "WHERE f.project_id=? AND f.status='matched' GROUP BY a.id", pid)}
        board = [{"agent": r["agent"], "compiles": r["compiles"], "matched": matched.pop(r["agent"], 0), "last": r["last"]}
                 for r in rows]
        board += [{"agent": a, "compiles": 0, "matched": n, "last": None} for a, n in matched.items()]
        board.sort(key=lambda r: (-r["matched"], -r["compiles"]))
        per_hour = [0] * 24
        for r in self.store.all("SELECT matched_at FROM functions WHERE project_id=? AND status='matched' AND matched_at > ?",
                                pid, now - 86400):
            per_hour[23 - min(23, max(0, int((now - r["matched_at"]) // 3600)))] += 1  # (one matched since `now`: this hour)
        compiles = self.store.one("SELECT COUNT(*) AS n FROM attempts t JOIN functions f ON f.id=t.function_id "
                                  "WHERE f.project_id=? AND t.created_at > ?", pid, now - 3600)["n"]
        return {"agents": board[:24], "matches_per_hour": per_hour, "compiles_last_hour": compiles}

    def session_counts(self, slug: str, agent: str, since: float) -> dict:
        """What one agent session did on this project since it started. Session names (claude-1, ...) come back
        after a restart, so the agent's all-time totals would count earlier sessions' work as this one's."""
        pid = self._project(slug)["id"]
        compiles = self.store.one(
            "SELECT COUNT(*) AS n FROM attempts t JOIN functions f ON f.id=t.function_id JOIN agents a ON a.id=t.agent_id "
            "WHERE f.project_id=? AND a.name=? AND t.created_at >= ?", pid, agent, since)["n"]
        matched = self.store.one(
            "SELECT COUNT(*) AS n FROM functions f JOIN agents a ON a.id=f.matched_agent_id "
            "WHERE f.project_id=? AND a.name=? AND f.status='matched' AND f.matched_at >= ?", pid, agent, since)["n"]
        return {"compiles": compiles, "matched": matched}

    def live(self, slug: str, since_event: int = 0, since_attempt: int = 0) -> dict:
        proj = self._project(slug)
        with self.store.tx() as db:
            self._expire_claims(db)
        events = self.store.all(
            "SELECT e.id, e.kind, e.detail, e.created_at, a.name AS agent FROM events e LEFT JOIN agents a "
            "ON a.id=e.agent_id WHERE e.project_id=? AND e.id>? ORDER BY e.id DESC LIMIT 60", proj["id"], since_event)
        attempts = self.store.all(
            "SELECT t.id, t.score, t.compiled, t.created_at, f.name AS function, f.n_insns, a.name AS agent "
            "FROM attempts t JOIN functions f ON f.id=t.function_id LEFT JOIN agents a ON a.id=t.agent_id "
            "WHERE f.project_id=? AND t.id>? ORDER BY t.id DESC LIMIT 80", proj["id"], since_attempt)
        claims = self.store.all(
            "SELECT f.name, f.n_insns, f.best_score, f.attempts, a.name AS agent, c.claimed_at FROM claims c "
            "JOIN functions f ON f.id=c.function_id JOIN agents a ON a.id=c.agent_id WHERE f.project_id=?", proj["id"])
        claims = [dict(c) for c in claims]
        if slug in self.links:
            for c in self.links[slug].claims:
                row = self.store.one("SELECT name, n_insns, best_score, attempts FROM functions WHERE project_id=? AND vram=?",
                                     proj["id"], c["vram"])
                if row:
                    claims.append(dict(row) | {"agent": "@" + c["agent"], "claimed_at": None})
        return {"events": [dict(e) for e in reversed(events)], "attempts": [dict(t) for t in reversed(attempts)],
                "claims": claims, "overview": self.overview(slug), **self._busy_elsewhere(slug, proj)}

    def _busy_elsewhere(self, slug: str, proj) -> dict:
        """Functions taken without a claim, for the map: the auto-fixer's current list and the ones researchers were
        handed (both by name)."""
        fixing = []
        if (fixer := getattr(self, "fixer", None)) is not None:
            try:
                fixing = list(fixer.status(slug).get("working_on") or [])
            except Exception:
                fixing = []
        now = time.time()
        with self._research_lock:
            held = [fid for fid, (_, until) in self._research_held.items() if until > now]
        researching = [r["name"] for r in self.store.all(
            f"SELECT name FROM functions WHERE project_id=? AND id IN ({','.join('?' * len(held))})",
            proj["id"], *held)] if held else []
        return {"fixing": fixing, "researching": researching}

    def set_compiler(self, slug: str, toolchain: str | None = None, flags: list[str] | None = None, *,
                     compiler_profiles=profiles.UNSET, compiler_ranges=profiles.UNSET,
                     compiler_functions=profiles.UNSET) -> None:
        """Change the default compiler (`toolchain`, `flags`) and/or the profile assignments: named profiles
        (toolchain + flags), VRAM ranges (global or per segment) and per-function overrides, see compiler_profiles.
        Everything is validated before anything is saved."""
        if self.fork_slug(slug):
            raise NetworkError("a fork builds with the project's own compilers and flags")
        proj = self._project(slug)
        cfg = json.loads(proj["config"])
        if cfg.get("link"):  # the saved link, not self.links: also before restore_links() ran
            raise NetworkError("this project follows the room host's compiler settings (the host re-checks every "
                               "match with them): ask the host to change them")
        before = toolchains.profile_of(proj)
        before_digest = snapshots.compiler_digest(proj)
        label: list[str] = []
        if toolchain is not None:
            tc = toolchains.get(toolchain)
            # a custom command template belongs to the old compiler; every other key (include_dirs,
            # ro_paths, sandbox, ...) still describes the project and is kept
            kept = {k: v for k, v in cfg.get("compiler", {}).items() if k != "cmd"}
            cfg["compiler"] = {**kept, **toolchains.profile_from(
                {**kept, "toolchain": tc.id, "flags": list(flags or tc.flags),
                 "provenance": toolchains.PROVENANCE_EXPLICIT}).to_dict()}
            label.append(f"{tc.name} {' '.join(cfg['compiler']['flags'])}")
        for key, value in (("compiler_profiles", compiler_profiles), ("compiler_ranges", compiler_ranges),
                           ("compiler_functions", compiler_functions)):
            if value is not profiles.UNSET:
                cfg[key] = value
                label.append(key.replace("_", " "))
        compiler = dict(cfg.get("compiler", {}))
        profile = toolchains.profile_from(compiler)
        change = toolchains.profile_diff(before, profile, note="set_compiler")
        # normalise the profile fields in place; keys the profile doesn't model (cmd, include_dirs, ro_paths,
        # sandbox, image, note) are kept, and a command-only config doesn't grow an empty toolchain
        cfg["compiler"] = {**compiler, **profile.to_dict()}
        if "toolchain" not in compiler and not profile.toolchain:
            cfg["compiler"].pop("toolchain", None)
        try:
            profiles.validate(cfg, {r["name"] for r in self.store.all(
                "SELECT name FROM functions WHERE project_id=?", proj["id"])})
        except (ValueError, toolchains.ToolchainError) as e:
            raise NetworkError(str(e))
        self._save_compiler(proj, cfg, change, before_digest, "compiler", '; '.join(label) or 'compiler')

    def _save_compiler(self, proj, cfg: dict, change, before_digest: str, kind: str, label: str) -> None:
        """Save a validated compiler change, drop what was made for the old compiler and record it."""
        # the compiler digest also covers keys the profile doesn't model (cmd, include_dirs, ...)
        identity_changed = change.is_identity_change or \
            snapshots.compiler_digest({"config": json.dumps(cfg)}) != before_digest
        with self.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
            if identity_changed:
                # drafts: old seeds are m2c output for the old profile
                db.execute("UPDATE functions SET seed=NULL WHERE project_id=? AND status='open'", (proj["id"],))
                # search budgets: a different compiler is new search input (bounded; reason recorded)
                db.execute(
                    "UPDATE search_budgets SET turns_used=0, cooldown_until=0, epoch=?, "
                    "reset_reason='compiler-changed', last_at=? WHERE (turns_used > 0 OR cooldown_until > 0) "
                    "AND function_id IN (SELECT id FROM functions WHERE project_id=?)",
                    (f"profile-{change.after_identity[:16]}", time.time(), proj["id"]))
            # The event carries the P4.1 invalidation contract: provenance, identity, what it invalidates.
            detail = (f"{label} (provenance {change.provenance[0]} -> "
                      f"{change.provenance[1]}; identity {change.before_identity[:12]} -> "
                      f"{change.after_identity[:12]}")
            if identity_changed:
                detail += f"; invalidated: {', '.join(change.invalidates or toolchains.INVALIDATED)}"
            detail += ")"
            self._event(db, proj["id"], None, kind, detail)

    def apply_remote_compiler(self, slug: str, settings: dict) -> bool:
        """Member side: compile with exactly the room host's compiler settings (compiler_profiles.shared). The host
        re-compiles every match with its own, so any difference (flags, assembler, a range or function override)
        turns a local match into a rejected one. This machine's own keys (include paths, sandbox, ...) are kept.
        Returns whether anything changed. A toolchain that isn't installed here is still applied: compiles then
        say so, instead of quietly using another compiler."""
        if not isinstance(settings, dict) or not isinstance(settings.get("compiler"), dict) or not settings["compiler"]:
            raise NetworkError("the room's host sent no compiler settings")
        proj = self._project(slug)
        cfg = json.loads(proj["config"])
        theirs = profiles.shared({"compiler": settings["compiler"], **{k: settings.get(k) for k in profiles.KEYS}})
        if profiles.shared(cfg) == theirs:
            return False
        before = toolchains.profile_of(proj)
        before_digest = snapshots.compiler_digest(proj)
        mine = {k: v for k, v in (cfg.get("compiler") or {}).items() if k in snapshots.COMPILE_KEYS}
        if theirs["compiler"].get("toolchain") != (cfg.get("compiler") or {}).get("toolchain"):
            mine.pop("cmd", None)  # a custom command template belongs to the old compiler (as in set_compiler)
        cfg["compiler"] = {**mine, **theirs["compiler"]}
        for key in profiles.KEYS:
            if theirs[key]:
                cfg[key] = theirs[key]
            else:
                cfg.pop(key, None)
        try:
            profile = toolchains.profile_from(cfg["compiler"])
            if profile.toolchain:
                toolchains.get(profile.toolchain)
            # function names aren't checked: the host did, and a rename may reach this copy a sync later
            profiles.validate(cfg)
        except (ValueError, TypeError, toolchains.ToolchainError) as e:
            raise NetworkError(f"can't use the room host's compiler settings here ({e}): "
                               "updating decomp.wtf to the host's version may help") from None
        missing = sorted({t for t in [profile.toolchain, *(p.get("toolchain") for p in theirs["compiler_profiles"].values())]
                          if t and toolchains.installed(t) is None})
        n = {k: len(theirs[k]) for k in profiles.KEYS}
        label = (f"compiler settings updated from the room's host: {profile.toolchain or 'command'} "
                 f"{' '.join(profile.flags)}".rstrip()
                 + (f", wrapper {theirs['compiler']['wrapper']}" if theirs["compiler"].get("wrapper") not in (None, "cc") else "")
                 + (f"; {n['compiler_profiles']} profiles, {n['compiler_ranges']} ranges, "
                    f"{n['compiler_functions']} function overrides" if any(n.values()) else "")
                 + (f"; not installed here yet: {', '.join(missing)} (install from Setup)" if missing else ""))
        self._save_compiler(proj, cfg, toolchains.profile_diff(before, profile, note="room host"), before_digest,
                            "collab", label)
        return True

    def chat_agent(self, base: str, client: str = "") -> tuple[Agent, str]:
        """An identity for an agent the user talks to (Claude Code, Codex, ... connected with `decomp mcp`): `base`,
        or base_2, base_3... when another session of the same agent is active right now (never base-2: that's the
        name of a helper session, see local_agent). `client` is the bridge process's own stable id (bridge.CLIENT_ID):
        a bridge daemon that restarts with the same client id gets its ORIGINAL row back instead of a suffixed one
        (the identity split-brain perf-test found), while two genuinely concurrent clients still get separate
        identities."""
        base = re.sub(r"[^A-Za-z0-9_.-]+", "-", base).strip("-")[:30] or "chat"
        for n in range(1, 100):
            name = base if n == 1 else f"{base}_{n}"
            row = self.store.one("SELECT id, origin, last_seen, bridge FROM agents WHERE name=?", name)
            same_client = bool(client) and row is not None and row["bridge"] == client
            if row is None or same_client or (row["origin"] == "chat" and (row["last_seen"] or 0) < time.time() - 600):
                break
        else:
            raise NetworkError("too many chat sessions at once")
        if row is None:
            if (why := _lockdown_refuses(name, self.db_path)):
                raise NetworkError(why)
            token = self.register_agent(name, "you", trusted=True, origin="chat")
            if client:
                with self.store.tx() as db:
                    db.execute("UPDATE agents SET bridge=? WHERE id=(SELECT id FROM agents WHERE name=?)",
                               (client, name))
        else:
            token = "dn_" + secrets.token_urlsafe(24)
            with self.store.tx() as db:
                db.execute("UPDATE agents SET token_hash=?, last_seen=?, bridge=COALESCE(?, bridge) WHERE id=?",
                           (_hash_token(token), time.time(), client or None, row["id"]))
        return self.authenticate(token), token
    def is_chat(self, agent: Agent) -> bool:
        row = self.store.one("SELECT origin FROM agents WHERE id=?", agent.id)
        return bool(row) and row["origin"] == "chat"

    def local_agent(self, name: str) -> tuple[Agent, str]:
        """An agent identity for one runner on this machine (created on first use). A name an agent the user talks
        to had before 0.10.4 (chat identities were numbered like helper sessions) becomes the runner's: a helper
        never gets the chat agent's tools, and the chat agent never takes over the runner's identity."""
        row = self.store.one("SELECT id FROM agents WHERE name=?", name)
        if row is not None:
            token = "dn_" + secrets.token_urlsafe(24)
            with self.store.tx() as db:
                db.execute("UPDATE agents SET token_hash=?, origin=CASE origin WHEN 'chat' THEN 'local' ELSE origin END "
                           "WHERE id=?", (_hash_token(token), row["id"]))
        else:
            if (why := _lockdown_refuses(name, self.db_path)):
                raise NetworkError(why)
            token = self.register_agent(name, "you", trusted=True, origin="local")
        return self.authenticate(token), token
