"""The third-party decomp and reverse-engineering tools in local-tools/ (disassemblers, decompilers, decompressors,
symbol finders, ROM and disc extractors), for the people and the agents of this decomp.wtf.

Each tool is a folder with tool.json and README.md; manifest.json lists them all. The programs themselves are built
on the computer (they aren't in git), so a tool can be listed here and still be missing.

Helper agents run with no shell on purpose, so they never run a tool directly: they ask for one of the operations
below, and the command line is built here from checked values only (numbers, hex, names from a fixed list, files
inside the project). Nothing an agent writes reaches a shell or a tool's own command prompt (rizin's `-c`, for
example, would run `!commands`, sandbox setting or not). Tools that only work with free-form input (scripts, config
files that name output paths, interactive debuggers) are listed with how a person uses them instead.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..network import NetworkError

OUTPUT = "tool-output"  # what tools write, per project and agent, outside the project (never committed or shared)
MAX_TEXT = 12_000


# ------------------------------------------------------------------ where they are

def _candidates() -> list[Path]:
    """DECOMP_LOCAL_TOOLS, the checkout this decomp.wtf runs from, and (when that checkout is a git worktree) the main
    checkout: the programs are built once, in one place, and aren't in git."""
    env = os.environ.get("DECOMP_LOCAL_TOOLS")
    repo = Path(__file__).resolve().parents[3]
    out = ([Path(env).expanduser()] if env else []) + [repo / "local-tools"]
    git = repo / ".git"
    if git.is_file():  # "gitdir: <main>/.git/worktrees/<name>"
        text = git.read_text(errors="replace").strip()
        if text.startswith("gitdir:"):
            gitdir = Path(text.split(":", 1)[1].strip())
            if gitdir.parent.name == "worktrees":
                out.append(gitdir.parent.parent.parent / "local-tools")
    return out


def folder() -> Path | None:
    """The local-tools/ to use: the first candidate with tools built in it, else the first with a manifest."""
    found = [c for c in _candidates() if (c / "manifest.json").is_file()]
    for cand in found:
        try:
            tools = json.loads((cand / "manifest.json").read_text(encoding="utf-8")).get("tools", [])
        except (OSError, ValueError):
            continue
        if any(_built(cand, t) for t in tools):
            return cand
    return found[0] if found else None


def manifest() -> list[dict]:
    root = folder()
    if root is None:
        return []
    try:
        return json.loads((root / "manifest.json").read_text(encoding="utf-8")).get("tools", [])
    except (OSError, ValueError):
        return []


def _tool(name: str) -> dict:
    for t in manifest():
        if t["name"] == name:
            return t
    raise NetworkError(f"no tool {name!r} here; local_tools() lists them")


def _ghidra_extension(t: dict) -> bool:
    return t.get("category") == "ghidra-extension"


# Tools that need a person (or a build script), and why: shown in the list instead of operations.
PEOPLE_ONLY = {
    "armips": "assembles .asm files that choose their own output paths; agents use keystone.assemble for bytes",
    "asm-differ": "reads a Python settings file from the project; decomp.wtf's compile_and_diff is the agents' diff",
    "asm-processor": "a compiler wrapper for build scripts (GLOBAL_ASM); decomp.wtf's judge compiles for agents",
    "mwccgap": "a compiler wrapper for build scripts (GLOBAL_ASM with mwcc); decomp.wtf's judge compiles for agents",
    "ido-static-recomp": "the IDO compiler recompiled for this computer: used by builds, not run on its own",
    "decomp-permuter": "runs the project's compile scripts; decomp.wtf's autopilot runs its own permuter",
    "n64recomp": "turns a whole N64 game into C for ports, from a config file that names its outputs",
    "torch": "extracts assets from a config file that names its outputs",
    "mgba": "an emulator and interactive debugger: for play-testing a GBA build",
    "unicorn": "a Python emulation library: for scripts a person writes",
    "gclib": "a Python library for GameCube/Wii file formats: for scripts a person writes",
    "ndspy": "a Python library for DS file formats: for scripts a person writes",
    "findcode": "its macOS build fails (see its README)",
}


# What a tool's operations actually start, beyond its manifest entry (a `run` wrapper is in git, built or not).
NEEDS = {
    "keystone": ["bin/kstool"], "rizin": ["bin/rizin", "bin/rz-asm", "bin/rz-bin"], "radare2": ["bin/rabin2"],
    "rz-ghidra": ["plugins/core_ghidra.dylib", "../rizin/bin/rizin"], "retdec": ["bin/retdec-decompiler"],
    "pypcode": [".venv/bin/python"], "pygfxd": [".venv/bin/python"], "mapfile-parser": [".venv/bin/python"],
    "ghidriff": [".venv/bin/ghidriff"], "mkpsxiso": ["bin/dumpsxiso"], "pret-gba-tools": ["bin/gbagfx"],
    "asm-differ": [".venv/bin/asm-differ"], "decomp-permuter": [".venv/bin/python", "app/permuter.py"],
    "gclib": [".venv/bin/python"], "ndspy": [".venv/bin/python"], "mwccgap": ["src/mwccgap.py"],
}


def _built(root: Path, t: dict) -> bool:
    base = root / t["folder"]
    return all((base / rel).exists() for rel in [t["entry"], *NEEDS.get(t["name"], [])])


def status(t: dict) -> tuple[str, str]:
    """("ready" | "not built" | "failing" | "ghidra", what it means) for one manifest entry."""
    root = folder()
    if t.get("status") != "pass":
        return "failing", "its smoke test fails on this computer"
    if root is None or not _built(root, t):
        return "not built", "its program isn't built on this computer (see its README to install it)"
    if _ghidra_extension(t):
        return "ghidra", "a Ghidra extension: install it in Ghidra (File > Install Extensions) to load these games"
    return "ready", ""


# ------------------------------------------------------------------ operations for agents

@dataclass
class Op:
    summary: str
    params: dict[str, str]
    argv: Callable[["_Run"], list[str]]
    timeout: int = 60
    result: str = ""  # a file (glob, in the output folder) whose text is the answer, instead of stdout


@dataclass
class _Run:
    """One run's checked inputs."""
    tool_dir: Path
    out: Path
    file: Path | None = None
    file2: Path | None = None
    values: dict = field(default_factory=dict)
    scratch: list[Path] = field(default_factory=list)  # removed after the run

    def __getitem__(self, k):
        return self.values[k]

    def bin(self, name: str) -> str:
        return str(self.tool_dir / name)


ARCHES = {  # name: (rizin arch, bits, endian, pypcode language)
    "mips-be": ("mips", 32, "big", "MIPS:BE:32:default"),
    "mips-le": ("mips", 32, "little", "MIPS:LE:32:default"),
    "ppc": ("ppc", 32, "big", "PowerPC:BE:32:default"),
    "arm": ("arm", 32, "little", "ARM:LE:32:v5t"),
    "thumb": ("arm", 16, "little", "ARM:LE:32:v5t"),
    "aarch64": ("arm", 64, "little", "AARCH64:LE:64:v8A"),
    "x86": ("x86", 32, "little", "x86:LE:32:default"),
    "sm83": ("gb", 8, "little", ""),
}
KEYSTONE_ARCHES = ["mipsbe", "mips", "mips64be", "mips64", "arm", "armbe", "thumb", "thumbbe", "arm64", "ppc32be",
                   "ppc64be", "x32", "x64"]
MICROCODES = ["f3dex2", "f3dex", "f3dexb", "f3d", "f3db"]


def _rizin(r: _Run, command: str) -> list[str]:
    arch, bits, endian, _ = ARCHES[r["arch"]]
    return ["-N", "-q", "-e", "scr.color=0", "-e", "scr.interactive=false", "-a", arch, "-b", str(bits),
            "-E", endian, "-m", hex(r["base"]), "-c", command, str(r.file)]


def _gbagfx(r: _Run) -> list[str]:
    """gbagfx goes by file extensions: the input gets one that says its format."""
    src = r.out / f"input.{r['format']}"
    shutil.copyfile(r.file, src)
    return [r.bin("bin/gbagfx"), str(src), str(r.out / "image.png"), *(["-width", str(r["width"])] if r["width"] else [])]


def _ghidriff(r: _Run) -> list[str]:
    """Ghidra refuses project paths with a folder starting with '.' (decomp.wtf's is .decomp): its project, symbols
    and caches go in a temporary folder, the report in the results."""
    work = Path(tempfile.mkdtemp(prefix="decomp-ghidriff-"))
    r.scratch.append(work)
    return [r.bin("bin/ghidriff"), str(r.file), str(r.file2), "-o", str(r.out), "-p", str(work / "projects"),
            "-s", str(work / "symbols"), "--gzfs-path", str(work / "gzfs")]


def _gbadisasm_cfg(r: _Run) -> list[str]:
    cfg = r.out / "rom.cfg"
    cfg.write_text(f"{'thumb' if r['mode'] == 'thumb' else 'arm'}_func {hex(r['address'])}\n")
    return [r.bin("bin/gbadisasm"), str(r.file), "-c", str(cfg)]


PCODE_SCRIPT = Path(__file__).with_name("localtools_pcode.py")

FILE = "a file of the project (path from its folder; 'game' = the game file itself; tool-output/... = an earlier result)"
OFFSET = "optional: start at this byte offset of the file (hex like 0x1234), with `length` bytes"

OPS: dict[str, dict[str, Op]] = {
    "keystone": {
        "assemble": Op("Assemble instructions into bytes (hex), to check what an instruction encodes to.",
                       {"arch": " | ".join(KEYSTONE_ARCHES), "text": "instructions, ';'-separated"},
                       lambda r: [r.bin("bin/kstool"), r["arch"], r["text"]]),
    },
    "rizin": {
        "disassemble_bytes": Op("Disassemble hex bytes.", {"arch": " | ".join(ARCHES), "hex": "bytes in hex",
                                                            "address": "optional address of the first byte"},
                                lambda r: [r.bin("bin/rz-asm"), "-a", ARCHES[r["arch"]][0], "-b", str(ARCHES[r["arch"]][1]),
                                           *(["-e"] if ARCHES[r["arch"]][2] == "big" else []), "-o", hex(r["address"]),
                                           "-d", r["hex"]]),
        "disassemble": Op("Disassemble `count` instructions of a file at an address (raw files are mapped at `base`).",
                          {"file": FILE, "arch": " | ".join(ARCHES), "address": "address to start at",
                           "count": "instructions (up to 400)", "base": "where the file is loaded (default 0)"},
                          lambda r: [r.bin("bin/rizin"), *_rizin(r, f"pd {r['count']} @ {hex(r['address'])}")]),
        "strings": Op("Printable strings in a file, with their offsets.", {"file": FILE},
                      lambda r: [r.bin("bin/rz-bin"), "-zz", str(r.file)]),
    },
    "rz-ghidra": {
        "decompile": Op("Ghidra's decompiler on one function (a draft to read, not matching code).",
                        {"file": FILE, "arch": " | ".join(ARCHES), "address": "the function's address",
                         "base": "where the file is loaded (default 0)"},
                        lambda r: [r.bin("run"), *_rizin(r, f"af @ {hex(r['address'])}; pdg @ {hex(r['address'])}")],
                        timeout=120),
    },
    "radare2": {
        "file_info": Op("What a binary is: format, architecture, sections, entry points, symbols.", {"file": FILE},
                        lambda r: [r.bin("bin/rabin2"), "-I", "-S", "-e", "-s", str(r.file)]),
    },
    "pypcode": {
        "pcode": Op("Ghidra p-code (what each instruction does, step by step) for hex bytes.",
                    {"arch": " | ".join(a for a, v in ARCHES.items() if v[3]), "hex": "bytes in hex",
                     "address": "optional address of the first byte"},
                    lambda r: [r.bin(".venv/bin/python"), "-I", str(PCODE_SCRIPT), ARCHES[r["arch"]][3], r["hex"],
                               hex(r["address"])]),
    },
    "retdec": {
        "decompile": Op("RetDec's decompiler on a whole ELF/object file (a draft to read).", {"file": FILE},
                        lambda r: [r.bin("run"), str(r.file), "-o", str(r.out / "decompiled.c")], timeout=300,
                        result="decompiled.c"),
    },
    "binwalk": {
        "scan": Op("Find compressed data, archives and known file formats inside a file.", {"file": FILE},
                   lambda r: [r.bin("bin/binwalk"), str(r.file)], timeout=120),
        "extract": Op("Find and unpack what binwalk recognises into tool-output.", {"file": FILE},
                      lambda r: [r.bin("bin/binwalk"), "-e", "-C", str(r.out), str(r.file)], timeout=300),
    },
    "crunch64": {
        "decompress": Op("Decompress Yay0 / Yaz0 / MIO0 / gzip data (N64).",
                         {"file": FILE, "format": "yay0 | yaz0 | mio0 | gzip", "offset": OFFSET},
                         lambda r: [r.bin("bin/crunch64"), "decompress", r["format"], str(r.file), str(r.out / "out.bin")]),
        "compress": Op("Compress a file to Yay0 / Yaz0 / MIO0 / gzip the way N64 games did.",
                       {"file": FILE, "format": "yay0 | yaz0 | mio0 | gzip"},
                       lambda r: [r.bin("bin/crunch64"), "compress", r["format"], str(r.file), str(r.out / "out.bin")]),
    },
    "n64sym": {
        "identify": Op("Name known N64 library functions (libultra and more) found in a ROM or RAM dump.",
                       {"file": FILE + " (default: the game)"},
                       lambda r: [r.bin("bin/n64sym"), str(r.file), "-s", "-f", "splat", "-o", str(r.out / "symbols.txt")],
                       timeout=180, result="symbols.txt"),
    },
    "gfxdis": {
        "display_list": Op("Disassemble an N64 display list (graphics commands) from hex or a file slice.",
                           {"microcode": " | ".join(MICROCODES), "hex": "the list in hex", "file": "or: " + FILE,
                            "offset": OFFSET},
                           lambda r: [r.bin(f"bin/gfxdis.{r['microcode']}"),
                                      *(["-f", str(r.file)] if r.file else ["-d", r["hex"]])]),
    },
    "pygfxd": {
        "display_list": Op("Disassemble an N64 display list file with pygfxd (C macros).",
                           {"file": FILE, "microcode": " | ".join(MICROCODES), "offset": OFFSET},
                           lambda r: [r.bin("run"), str(r.file), r["microcode"]]),
    },
    "pigment64": {
        "to_png": Op("Turn N64 texture bytes into a PNG.",
                     {"file": FILE, "format": "ci4 | ci8 | i1 | i4 | i8 | ia4 | ia8 | ia16 | rgba16 | rgba32",
                      "width": "pixels", "height": "pixels", "file2": "palette file, for ci4/ci8", "offset": OFFSET},
                     lambda r: [r.bin("bin/pigment64"), "to-png", "--width", str(r["width"]), "--height",
                                str(r["height"]), "-f", r["format"], "-o", str(r.out / "image.png"),
                                *(["-p", str(r.file2)] if r.file2 else []), str(r.file)]),
    },
    "pret-gba-tools": {
        "to_png": Op("Turn GBA tile graphics (1/4/8 bpp) into a PNG with gbagfx.",
                     {"file": FILE, "format": "1bpp | 4bpp | 8bpp", "width": "optional width in tiles", "offset": OFFSET},
                     _gbagfx),
    },
    "gbadisasm": {
        "disassemble": Op("Disassemble GBA code by following it from a function (ARM or Thumb).",
                          {"file": FILE + " (default: the game)", "address": "the function, like 0x08000100",
                           "mode": "thumb | arm"},
                          _gbadisasm_cfg, timeout=120),
    },
    "ccc": {
        "symbols": Op("PS2 ELF symbol tables (STABS/mdebug): functions, types, globals, or which compiler made it.",
                      {"file": FILE, "what": "functions | types | globals | identify"},
                      lambda r: [r.bin("bin/stdump"), r["what"], str(r.file)]),
    },
    "pcsx-redux-tools": {
        "psyq_object": Op("Dump a PlayStation PSY-Q .OBJ/.LIB object: sections, symbols, relocations.", {"file": FILE},
                          lambda r: [r.bin("bin/psyq-obj-parser"), str(r.file), "-d"]),
    },
    "mkpsxiso": {
        "extract": Op("Unpack a PlayStation disc image (.bin/.iso) into tool-output, with its layout as XML.",
                      {"file": FILE},
                      lambda r: [r.bin("bin/dumpsxiso"), "-x", str(r.out / "files"), "-s", str(r.out / "disc.xml"),
                                 str(r.file)], timeout=600),
    },
    "ndstool": {
        "info": Op("A DS ROM's header: title, code, ARM9/ARM7 addresses and sizes.", {"file": FILE},
                   lambda r: [r.bin("bin/ndstool"), "-i", str(r.file)]),
    },
    "ds-rom": {
        "extract": Op("Unpack a DS ROM (ARM9, overlays, files) into tool-output.", {"file": FILE},
                      lambda r: [r.bin("bin/dsrom"), "extract", "--rom", str(r.file), "--path", str(r.out / "rom")],
                      timeout=300),
    },
    "nodtool": {
        "info": Op("A GameCube/Wii disc's header and partitions.", {"file": FILE},
                   lambda r: [r.bin("bin/nodtool"), "info", str(r.file)]),
        "extract": Op("Unpack a GameCube/Wii disc into tool-output (large discs take a while).", {"file": FILE},
                      lambda r: [r.bin("bin/nodtool"), "extract", str(r.file), str(r.out / "disc")], timeout=900),
    },
    "mapfile-parser": {
        "symbols": Op("A linker map file as JSON: every segment, file and symbol with addresses and sizes.",
                      {"file": FILE}, lambda r: [r.bin("run"), "jsonify", str(r.file)]),
        "symbol": Op("Where one symbol is in a linker map file.", {"file": FILE, "text": "the symbol"},
                     lambda r: [r.bin("run"), "sym_info", str(r.file), r["text"]]),
    },
    "slinky": {
        "linker_script": Op("Generate a linker script from a slinky YAML description.", {"file": FILE},
                            lambda r: [r.bin("bin/slinky-cli"), str(r.file)]),
    },
    "ghidriff": {
        "diff": Op("Compare two builds (ELF/objects) function by function with Ghidra: what changed.",
                   {"file": FILE, "file2": "the other " + FILE},
                   lambda r: _ghidriff(r), timeout=900, result="*.ghidriff.md"),
    },
}


# ------------------------------------------------------------------ checking what an agent asked for

def _number(v, what: str, lo: int = 0, hi: int = 1 << 64) -> int:
    try:
        n = v if isinstance(v, int) else int(str(v).strip(), 0)
    except ValueError:
        raise NetworkError(f"{what} must be a number (decimal or 0x hex), not {v!r}") from None
    if not lo <= n < hi:
        raise NetworkError(f"{what} must be between {lo} and {hi - 1}")
    return n


def _choice(v: str, options, what: str) -> str:
    v = (v or "").strip().lower()
    if v not in options:
        raise NetworkError(f"{what} must be one of: {', '.join(options)}")
    return v


def _hex(v: str) -> str:
    h = re.sub(r"\s+", "", v or "")
    if h.lower().startswith("0x"):
        h = h[2:]
    if not h or len(h) % 2 or len(h) > 8192 or not re.fullmatch(r"[0-9a-fA-F]+", h):
        raise NetworkError("hex must be an even number of hex digits (up to 4096 bytes)")
    return h


def _in_project(root: Path, game: Path | None, v: str, results: Path | None = None) -> Path:
    v = (v or "").strip()
    if v in ("", "game", "rom"):
        if game is None or not game.is_file():
            raise NetworkError("this project has no single game file; name a file of the project instead")
        return game
    if results is not None and (v == OUTPUT or v.startswith(OUTPUT + "/")):
        base, p = results, (results / v[len(OUTPUT):].lstrip("/")).resolve()
    else:
        base, p = root, Path(v)
        p = (p if p.is_absolute() else root / p).resolve()
    if not p.is_relative_to(base.resolve()):
        raise NetworkError(f"{v!r} is outside the project folder: tools only read the project's own files")
    if not p.is_file():
        raise NetworkError(f"no file {v!r} in the project folder")
    return p


def _project_files(net, slug: str) -> tuple[Path, Path | None]:
    proj = net.project_row(slug)
    root = Path(proj["root"])
    rom = (net.cfg(proj).get("rom") or {}).get("file")
    return root, (root / rom if rom else None)


def _slice(src: Path, out: Path, offset: int, length: int | None) -> Path:
    data = src.read_bytes()
    if offset >= len(data):
        raise NetworkError(f"offset {hex(offset)} is past the end of the file ({len(data)} bytes)")
    piece = data[offset:offset + length] if length else data[offset:]
    dest = out / f"input.bin"
    dest.write_bytes(piece)
    return dest


def results_folder(net, slug: str, agent: str) -> Path:
    """Where one agent's tool results go: in decomp.wtf's own folder, not the project's (a fork's folder is a git
    checkout that gets committed, and results can hold pieces of the game). One per agent, so agents working at the
    same time don't overwrite each other's."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", agent or "you")[:60] or "you"
    return Path(net.workspace) / ".decomp" / OUTPUT / re.sub(r"[^A-Za-z0-9_.-]", "_", slug) / safe


# ------------------------------------------------------------------ running

def _execute(argv: list[str], cwd: Path, timeout: int) -> tuple[int | None, str]:
    """Runs one command (never through a shell) in its own process group, so a timeout stops all of it."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER",
                                                       "JAVA_HOME_21", "GHIDRA_INSTALL_DIR")}
    env["PATH"] = os.pathsep.join([env.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"])
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, _ = proc.communicate()
        return None, out.decode("utf-8", "replace")


def _cap(text: str, limit: int = MAX_TEXT) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)  # colour codes
    text = "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))  # progress bars redrawn in place
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text if len(text) <= limit else text[:limit] + f"\n... ({len(text) - limit:,} more characters cut)"


def run(net, slug: str, tool: str, operation: str, agent: str = "you", **args) -> str:
    """Runs one operation of one tool on the project's files, for an agent. Its files go to the agent's results
    folder, which it names as tool-output/... ."""
    t = _tool(tool)
    state, why = status(t)
    if state != "ready":
        raise NetworkError(f"{tool} isn't usable here: {why}")
    ops = OPS.get(tool)
    if not ops:
        raise NetworkError(f"{tool} has no operations for agents: {PEOPLE_ONLY.get(tool, 'it is for people')}")
    op = ops.get((operation or "").strip())
    if op is None:
        raise NetworkError(f"{tool} can: {', '.join(ops)} (local_tools('{tool}') explains them)")

    root, game = _project_files(net, slug)
    results = results_folder(net, slug, agent)
    out = results / f"{tool}-{operation}"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    p = op.params
    values: dict = {}
    r = _Run(folder() / t["folder"], out)
    if "arch" in p:
        values["arch"] = _choice(args.get("arch", ""), KEYSTONE_ARCHES if tool == "keystone" else
                                 [a for a in ARCHES if tool != "pypcode" or ARCHES[a][3]], "arch")
    if "format" in p:
        values["format"] = _choice(args.get("format", ""), [s.strip() for s in p["format"].split("|")], "format")
    if "microcode" in p:
        values["microcode"] = _choice(args.get("microcode") or "f3dex2", MICROCODES, "microcode")
    if "what" in p:
        values["what"] = _choice(args.get("what") or "functions", ["functions", "types", "globals", "identify"], "what")
    if "mode" in p:
        values["mode"] = _choice(args.get("mode") or "thumb", ["thumb", "arm"], "mode")
    if "text" in p:
        text = str(args.get("text") or "")
        if not text.strip() or len(text) > 4000 or "\0" in text or text.lstrip().startswith("-"):
            raise NetworkError("text is required (up to 4000 characters, not starting with '-')")
        values["text"] = text
    values["address"] = _number(args.get("address") or 0, "address")
    values["base"] = _number(args.get("base") or 0, "base")
    values["count"] = _number(args.get("count") or 40, "count", 1, 401)
    values["width"] = _number(args.get("width") or 0, "width", 0, 4097)
    values["height"] = _number(args.get("height") or 0, "height", 0, 4097)
    if "width" in p and "height" in p and not (values["width"] and values["height"]):
        raise NetworkError("width and height are required (pixels)")
    if tool == "gbadisasm" and not values["address"]:
        raise NetworkError("address is required")
    if "hex" in p:
        if args.get("hex") or "file" not in p:
            values["hex"] = _hex(args.get("hex", ""))
    if "file" in p and not values.get("hex"):
        r.file = _in_project(root, game, args.get("file", ""), results)
        if "offset" in p and (args.get("offset") or args.get("length")):
            r.file = _slice(r.file, out, _number(args.get("offset") or 0, "offset"),
                            _number(args.get("length") or 0, "length") or None)
    if "file2" in p and args.get("file2"):
        r.file2 = _in_project(root, game, args["file2"], results)
    elif "file2" in p and tool == "ghidriff":
        raise NetworkError("ghidriff needs two files: file and file2")
    r.values = values

    try:
        code, text = _execute(op.argv(r), out, op.timeout)
    finally:
        for d in r.scratch:
            shutil.rmtree(d, ignore_errors=True)
    found = sorted(out.glob(op.result)) if op.result else []
    if found:
        answer = found[0].read_text(encoding="utf-8", errors="replace")
        text = (answer or "(nothing found)") + ("\n" + text if code else "")
    for base, shown in ((results, OUTPUT + "/"), (root, "")):
        text = text.replace(str(base.resolve()) + "/", shown).replace(str(base) + "/", shown)
    made = sorted(f for f in out.rglob("*") if f.is_file() and f.stem != "input" and f.name != "rom.cfg")
    head = (f"{tool} {operation}: " + ("timed out after " + str(op.timeout) + " s" if code is None
                                       else "ok" if code == 0 else f"exit code {code}"))
    files = ""
    if made:
        listed = [f"  {OUTPUT}/{f.relative_to(results)} ({f.stat().st_size:,} bytes)" for f in made[:40]]
        more = f"\n  ... and {len(made) - 40} more" if len(made) > 40 else ""
        files = "\nFiles written (pass them as `file` to another tool):\n" + "\n".join(listed) + more
    return f"{head}\n{_cap(text.strip()) or '(no output)'}{files}"


# ------------------------------------------------------------------ describing them

def describe(tool: str = "") -> str:
    """For agents: every tool with what it's for and what they can run, or one tool in detail."""
    root = folder()
    if root is None:
        return "No local tools on this computer (decomp.wtf looks for a local-tools folder with manifest.json)."
    if tool:
        t = _tool(tool)
        state, why = status(t)
        readme = (root / t["folder"] / "README.md")
        lines = [f"{t['name']} ({t['category']}): {state}{' - ' + why if why else ''}"]
        for name, op in OPS.get(tool, {}).items():
            params = "\n".join(f"      {k}: {v}" for k, v in op.params.items())
            lines.append(f"  run_tool(tool='{tool}', operation='{name}', ...): {op.summary}\n{params}")
        if tool in PEOPLE_ONLY:
            lines.append(f"  Not for agents: {PEOPLE_ONLY[tool]}.")
        if readme.is_file():
            lines.append("\n" + _cap(readme.read_text(encoding="utf-8", errors="replace"), 4000))
        return "\n".join(lines)
    usable, other = [], []
    for t in manifest():
        state, why = status(t)
        ops = OPS.get(t["name"], {})
        if state == "ready" and ops:
            usable.append(f"- {t['name']}: " + "; ".join(f"{n} ({o.summary.rstrip('.')})" for n, o in ops.items()))
        else:
            other.append(f"- {t['name']}: " + (PEOPLE_ONLY.get(t["name"]) or why or "for people"))
    return ("Tools you can run with run_tool(tool, operation, ...) on the project's files (local_tools(tool) shows "
            "the parameters). Files they write are yours as tool-output/..., to pass to the next tool:\n"
            + "\n".join(usable)
            + ("\n\nAlso on this computer, for people (not runnable by agents):\n" + "\n".join(other) if other else ""))


def summary() -> list[dict]:
    """For the GUI's Compilers & tools page."""
    out = []
    for t in manifest():
        state, why = status(t)
        out.append({"name": t["name"], "category": t.get("category", ""), "repo": t.get("repo", ""),
                    "version": t.get("version", ""), "state": state, "why": why,
                    "agents": sorted(OPS.get(t["name"], {})) if state == "ready" else [],
                    "people": PEOPLE_ONLY.get(t["name"], "")})
    return out


def test(tool: str) -> tuple[bool, str]:
    """Runs a tool's own smoke test (test.sh), for the GUI's Check button."""
    t = _tool(tool)
    script = folder() / t["folder"] / "test.sh"
    if not script.is_file():
        return False, "no test for this tool here"
    code, text = _execute(["bash", str(script)], script.parent, 300)
    return code == 0, _cap(text.strip(), 3000)
