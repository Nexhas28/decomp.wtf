"""Start a GameCube or Wii decompilation from scratch: a dtk-template project made for the user's own game.

dtk-template is the community's standard project layout (CC0). decomp-toolkit analyses the game's program, finds
every function and splits the program into objects; the project then rebuilds the program byte for byte (checked
against its SHA-1) on every build, with decompiled source linked in as units match. That makes the result the base
for ports and mods from the first minute, and the same kind of project as the GameCube and Wii decomps on
decomp.dev, so it can be shared and continued anywhere.

The game's REL modules (code loaded at run time: Mario Party's boards and minigames are 99 of them) are analysed
with it, as the community's projects do: often most of a game's functions are in them.

Each code region dtk finds gets a source file (src/auto/text_<address>.c; a module's in src/auto/<module>/) for
agents to write into; decomp.wtf's
fork engine then works on it like on any community project. Tools are decomp.wtf's pinned copies (dtk, binutils,
CodeWarrior via wibo, objdiff, sjiswrap), so the build is offline and checksum-verified.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import toolchains
from ..limits import capped
from .project import ForkError, ForkProject

GC_DEFAULT, WII_DEFAULT = "GC/1.3.2", "Wii/1.0"
REL_EMPTY = "REL/empty.c"
DISC_MAGIC = {b"RVZ\x01": "rvz", b"WIA\x01": "wia", b"WBFS": "wbfs", b"CISO": "ciso"}
GCZ_MAGIC = 0xB10BC001


def comment_version(compiler: str) -> int:
    """The `.comment` version dtk writes into split objects, from the CodeWarrior version."""
    if compiler.startswith("Wii"):
        return 14
    m = re.match(r"GC/(\d+)\.(\d+)", compiler)
    major, minor = (int(m.group(1)), int(m.group(2))) if m else (1, 3)
    if major >= 3:
        return 14
    if (major, minor) >= (2, 7):
        return 11
    if (major, minor) >= (1, 3) and "1.3" != compiler[3:]:
        return 10
    return 8


def is_dol(data: bytes) -> bool:
    """A GameCube/Wii program (DOL): 7 text + 11 data section offsets, addresses in 0x80000000-0x81800000."""
    if len(data) < 0x100:
        return False
    offs = struct.unpack(">18I", data[0:72])
    addrs = struct.unpack(">18I", data[72:144])
    sizes = struct.unpack(">18I", data[144:216])
    entry = struct.unpack(">I", data[0xE0:0xE4])[0]
    used = [(o, a, s) for o, a, s in zip(offs, addrs, sizes) if s]
    if not used or not 0x80000000 <= entry < 0x81800000:
        return False
    return all(0x100 <= o and o + s <= len(data) and 0x80000000 <= a < 0x81800000 for o, a, s in used)


def disc_kind(head: bytes) -> str | None:
    """GameCube/Wii disc image formats dtk reads (ISO/GCM by magic, compressed formats by their headers)."""
    if len(head) >= 0x20 and struct.unpack(">I", head[0x1C:0x20])[0] == 0xC2339F3D:
        return "gc"
    if len(head) >= 0x1C and struct.unpack(">I", head[0x18:0x1C])[0] == 0x5D1C9EA3:
        return "wii"
    if head[:4] in DISC_MAGIC or (len(head) >= 4 and struct.unpack("<I", head[:4])[0] == GCZ_MAGIC):
        return "packed"
    return None


def _dtk() -> str:
    toolchains.ensure("dtk", log=lambda *_: None)
    return str(toolchains.binary("dtk"))


def _vfs_read(disc: Path, inner: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="decompwtf-disc-") as tmp:
        out = Path(tmp) / Path(inner).name
        res = subprocess.run(capped([_dtk(), "vfs", "cp", f"{disc}:{inner}", str(out)]), capture_output=True, text=True,
                             timeout=600, encoding="utf-8", errors="replace")
        if res.returncode != 0 or not out.exists():
            raise ForkError(f"couldn't read {inner} from the disc image: {(res.stderr or res.stdout).strip()[-300:]}")
        return out.read_bytes()


def disc_id(path: Path) -> str | None:
    """The game ID (e.g. GP6E01) of a GameCube/Wii disc image in any format dtk reads, or None if it isn't one."""
    try:
        with open(path, "rb") as f:
            head = f.read(0x440)
        kind = disc_kind(head)
        if kind is None:
            return None
        boot = head if kind in ("gc", "wii") else _vfs_read(path, "sys/boot.bin")
    except (OSError, ForkError):
        return None
    gid = boot[:6].decode("ascii", "replace")
    return gid if re.fullmatch(r"[A-Z0-9]{6}", gid) else None


def vfs_copy(disc: Path, inner: list[str], dest: Path) -> None:
    """Copy files (paths inside the disc, like files/dll/m699Dll.rel) out of a disc image into `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    for i in range(0, len(inner), 64):
        res = subprocess.run(capped([_dtk(), "vfs", "cp", "-q", *[f"{disc}:{x}" for x in inner[i:i + 64]], f"{dest}{os.sep}"]),
                             capture_output=True, text=True, timeout=1800, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            raise ForkError(f"couldn't copy files out of the disc image: {(res.stderr or res.stdout).strip()[-400:]}")


def vfs_list(disc: Path) -> list[str]:
    """Every file in a disc image (paths like files/dll/m302Dll.rel)."""
    res = subprocess.run(capped([_dtk(), "vfs", "ls", "-r", "-s", f"{disc}:"]), capture_output=True, text=True,
                         timeout=600, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise ForkError(f"couldn't list the disc image's files: {(res.stderr or res.stdout).strip()[-300:]}")
    return [line.strip() for line in res.stdout.splitlines() if line.strip() and not line.strip().endswith("/")]


def is_rel(data: bytes) -> bool:
    """A REL module: a small module id, up to 32 sections whose table follows the 0x40-byte header (version 1-3)."""
    if len(data) < 0x40:
        return False
    module_id, _, _, sections, table = struct.unpack(">IIIII", data[:0x14])
    version = struct.unpack(">I", data[0x1C:0x20])[0]
    return 0 < module_id < 0x10000 and 0 < sections <= 32 and 0x40 <= table < len(data) and 1 <= version <= 3


def disc_modules(disc: Path) -> list[tuple[str, str, str, int]]:
    """The REL modules on a disc: (name, path in the disc, sha1, module id), in the disc's order. Compressed ones
    (inside archives, or Yaz0) aren't read yet."""
    rels = [f for f in vfs_list(disc) if f.lower().endswith(".rel")]
    if not rels:
        return []
    out, names = [], set()
    unique = len({Path(r).name for r in rels}) == len(rels)
    with tempfile.TemporaryDirectory(prefix="decompwtf-rels-") as tmp:
        if unique:  # all at once; else one folder each (two modules may share a file name in different folders)
            vfs_copy(disc, rels, Path(tmp) / "all")
        for i, rel in enumerate(rels):
            if not unique:
                vfs_copy(disc, [rel], Path(tmp) / str(i))
            data = (Path(tmp) / ("all" if unique else str(i)) / Path(rel).name).read_bytes()
            if not is_rel(data):
                continue
            name = re.sub(r"[^A-Za-z0-9_]", "_", Path(rel).stem) or f"module{i}"
            while name in names:
                name += "_"
            names.add(name)
            out.append((name, rel, hashlib.sha1(data).hexdigest(), struct.unpack(">I", data[:4])[0]))
    return out


def game_info(path: Path) -> dict:
    """What the user dropped: {"platform": gc|wii, "id", "title", "kind": dol|disc, "dol": bytes}."""
    with open(path, "rb") as f:
        head = f.read(0x440)
    if is_dol(path.read_bytes() if path.stat().st_size < 64 << 20 else head):
        dol = path.read_bytes()
        stem = re.sub(r"[^A-Z0-9]", "", path.stem.upper())[:6]
        game_id = stem if len(stem) >= 4 and stem != "MAIN" else "G" + hashlib.sha1(dol).hexdigest()[:5].upper()
        return {"platform": "gc", "id": game_id, "title": path.stem, "kind": "dol", "dol": dol}
    kind = disc_kind(head)
    if kind is None:
        raise ForkError("this isn't a GameCube or Wii disc image or program (DOL)")
    boot = head if kind in ("gc", "wii") else _vfs_read(path, "sys/boot.bin")
    platform = "wii" if struct.unpack(">I", boot[0x18:0x1C])[0] == 0x5D1C9EA3 else "gc"
    game_id = boot[:6].decode("ascii", "replace")
    if not re.fullmatch(r"[A-Z0-9]{6}", game_id):
        raise ForkError("the disc header has no valid game ID")
    title = boot[0x20:0x400].split(b"\0", 1)[0].decode("ascii", "replace").strip() or game_id
    return {"platform": platform, "id": game_id, "title": title, "kind": "disc", "dol": _vfs_read(path, "sys/main.dol")}


def tool_args() -> list[str]:
    """configure.py arguments pointing at decomp.wtf's pinned tools."""
    for tid in ("dtk", "gc-wii-binutils", "objdiff-cli", "sjiswrap", "wibo"):
        if tid != "wibo" or toolchains.get("wibo").supported:
            toolchains.ensure(tid, log=lambda *_: None)
    if toolchains.installed("mwcc") is None:
        raise ForkError("GameCube and Wii games are built with CodeWarrior, which is proprietary: install it in Setup "
                        "(Metrowerks CodeWarrior) if you accept that, then try again")
    args = ["--dtk", str(toolchains.binary("dtk")), "--binutils", str(toolchains.path("gc-wii-binutils")),
            "--compilers", str(toolchains.path("mwcc")), "--objdiff", str(toolchains.binary("objdiff-cli")),
            "--sjiswrap", str(toolchains.binary("sjiswrap"))]
    if toolchains.get("wibo").supported:
        args += ["--wrapper", str(toolchains.binary("wibo"))]
    return args


TYPES_H = """/* Basic types for this decompilation (CodeWarrior conventions). Agents add structs and prototypes here or in
 * new headers next to it. */
#ifndef COMMON_H
#define COMMON_H

typedef signed char s8;
typedef unsigned char u8;
typedef signed short s16;
typedef unsigned short u16;
typedef signed long s32;
typedef unsigned long u32;
typedef signed long long s64;
typedef unsigned long long u64;
typedef float f32;
typedef double f64;
typedef int BOOL;
#define TRUE 1
#define FALSE 0
#ifndef NULL
#define NULL ((void*)0)
#endif

#endif
"""


def _libs_block(compiler: str, sources: list[str], rels: bool = False) -> str:
    """The program's source files, and the REL modules' (auto/<module>/...: compiled without small data, as RELs
    are, since a module can't reach the program's small data sections)."""
    def lib(name: str, cflags: str, files: list[str]) -> str:
        objects = "".join(f'            Object({"Matching" if s == REL_EMPTY else "NonMatching"}, "{s}"),\n' for s in files)
        return f'''    {{
        "lib": "{name}",
        "mw_version": config.linker_version,
        "cflags": {cflags},
        "progress_category": "game",
        "objects": [
{objects}        ],
    }},
'''
    main = [s for s in sources if s.count("/") == 1]
    modules = ([REL_EMPTY] if rels else []) + [s for s in sources if s.count("/") > 1]
    return "config.libs = [\n" + lib("game", "cflags_base", main) + (lib("modules", "cflags_rel", modules) if modules else "") + "]\n\n\n"


def _write_configure(root: Path, game_id: str, compiler: str, sources: list[str], rels: bool = False) -> None:
    p = root / "configure.py"
    s = p.read_text(encoding="utf-8")
    s = re.sub(r'VERSIONS = \[\n.*?\n\]', f'VERSIONS = [\n    "{game_id}",  # 0\n]', s, count=1, flags=re.S)
    s = re.sub(r'^config\.linker_version = ".*"$', f'config.linker_version = "{compiler}"', s, count=1, flags=re.M)
    if "config.shift_jis = False" not in s:  # sources are plain C (turn it back on for Japanese string literals)
        s = s.replace(f'config.linker_version = "{compiler}"', f'config.linker_version = "{compiler}"\nconfig.shift_jis = False', 1)
    if rels and "config.rel_empty_file" not in s:  # what an empty module is linked from (marked Matching)
        s = s.replace("config.shift_jis = False", f'config.shift_jis = False\nconfig.rel_empty_file = "{REL_EMPTY}"', 1)
        (root / "src" / REL_EMPTY).parent.mkdir(parents=True, exist_ok=True)
        (root / "src" / REL_EMPTY).write_text("", encoding="utf-8")
    start = s.index("config.libs = [")
    end = s.index("# Optional callback to adjust link order")
    p.write_text(s[:start] + _libs_block(compiler, sources, rels) + s[end:], encoding="utf-8")


def _build(fp: ForkProject, log) -> tuple[bool, str]:
    ok, tail = fp.run([sys.executable, "configure.py", *tool_args()], log=log)
    if not ok:
        return False, tail
    return fp.run(["ninja"], log=log, timeout=3600)


_SECTION_LINE = re.compile(r"^\t(\S+)(\s+type:\w+.*)$")


def _unique_sections(root: Path, game_id: str) -> list[str]:
    """dtk names the program's sections from their kind and order. A game whose linker script added a section of its
    own (Mario Kart: Double Dash!! keeps archives in a .BINARY section before .rodata) gets two of one name, which dtk
    writes into splits.txt and then refuses to read back ("Multiple sections with name .rodata"). Keep the usual name
    on the last of them, where the standard layout has it, give the others names of their own, and let dtk analyse
    the program again with those names (its symbols name sections too, so they start over). Returns the new names."""
    renamed = []
    for path in [root / "config" / game_id / "splits.txt", *sorted((root / "config" / game_id / "rels").glob("*/splits.txt"))]:
        renamed += _unique_in(path)
    return renamed


# CodeWarrior's REL modules keep their sections at fixed indices; dtk calls one it can't place ".section<i>"
REL_SECTIONS = {1: (".text", "code"), 2: (".ctors", "rodata"), 3: (".dtors", "rodata"), 4: (".rodata", "rodata"),
                5: (".data", "data"), 6: (".bss", "bss")}


def _standard_module_sections(root: Path, game_id: str) -> list[str]:
    """A module section dtk couldn't name (Mario Party 4's nisDll: 16 bytes of constants as ".section4", type data)
    is left out of the rebuilt module. Give it CodeWarrior's name for its index, when that name isn't taken, and let
    dtk analyse the module again. Returns the names given."""
    named = []
    for path in sorted((root / "config" / game_id / "rels").glob("*/splits.txt")):
        text = path.read_text(encoding="utf-8")
        m = re.search(r"^Sections:\n((?:\t.*\n?)+)", text, re.M)
        if not m:
            continue
        lines = m.group(1).rstrip("\n").split("\n")
        names = [(_SECTION_LINE.match(line) or [None, None])[1] for line in lines]
        changed = False
        for i, name in enumerate(names):
            k = re.fullmatch(r"\.section(\d+)", name or "")
            standard = REL_SECTIONS.get(int(k.group(1))) if k else None
            if standard and standard[0] not in names:
                align = re.search(r"align:\d+", lines[i])
                lines[i] = f"\t{standard[0]:<11} type:{standard[1]}" + (f" {align.group(0)}" if align else "")
                names[i] = standard[0]
                named.append(f"{path.parent.name} {standard[0]}")
                changed = True
        if changed:
            path.write_text("Sections:\n" + "\n".join(lines) + "\n", encoding="utf-8")
            (path.parent / "symbols.txt").unlink(missing_ok=True)
    return named


def _unique_in(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r"^Sections:\n((?:\t.*\n?)+)", text, re.M)
    if not m:
        return []
    lines = m.group(1).rstrip("\n").split("\n")
    names = [(_SECTION_LINE.match(line) or [None, None])[1] for line in lines]
    renamed = []
    for i, name in enumerate(names):
        if name and names.count(name) > 1 and name in names[i + 1:]:
            new, k = f"{name}{i}", i  # letters and digits only: dtk reads no other section names in symbols.txt
            while new in names:
                k += 1
                new = f"{name}{k}"
            names[i] = new
            lines[i] = f"\t{new:<11} {_SECTION_LINE.match(lines[i]).group(2).strip()}"  # dtk's own layout
            renamed.append(new)
    if renamed:
        path.write_text("Sections:\n" + "\n".join(lines) + "\n", encoding="utf-8")
        (path.parent / "symbols.txt").unlink(missing_ok=True)
    return renamed


def _name_entry(root: Path, game_id: str, dol: bytes) -> bool:
    """CodeWarrior links from __start: make sure the program's entry point carries that name."""
    path = root / "config" / game_id / "symbols.txt"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if re.search(r"^__start = ", text, re.M):
        return False
    entry = struct.unpack(">I", dol[0xE0:0xE4])[0]
    new, n = re.subn(rf"^\w+ = (\.init|\.text):0x{entry:08X};", rf"__start = \1:0x{entry:08X};", text, count=1, flags=re.M)
    if n:
        path.write_text(new, encoding="utf-8")
    return bool(n)


def _code_units(root: Path, game_id: str) -> list[tuple[str | None, int, int, str]]:
    """(module or None for the program, start, end, section) of the code regions dtk split out automatically (not
    .init: hand-written start-up). A module's addresses are offsets in its section, as its splits.txt has them."""
    from elftools.elf.elffile import ELFFile

    cfg = json.loads((root / "build" / game_id / "config.json").read_text(encoding="utf-8"))
    out = []
    for module, units in [(None, cfg.get("units") or []),
                          *[(m.get("name"), m.get("units") or []) for m in (cfg.get("modules") or [])]]:
        for unit in units:
            m = re.match(r"auto_\d+_([0-9A-F]{8})_(\w+)$", unit.get("name", ""))
            if not m or m.group(2) != "text":
                continue
            with open(root / unit["object"], "rb") as f:
                sec = ELFFile(f).get_section_by_name(".text")
                size = sec["sh_size"] if sec is not None else 0
            if size:
                start_ = int(m.group(1), 16)
                out.append((module, start_, start_ + size, ".text"))
    return sorted(out, key=lambda u: (u[0] or "", u[1]))


def create(root: Path, game: Path, *, compiler: str | None = None, log=print) -> dict:
    """Make the project in `root` (which must not exist yet) and build it once. Returns facts about the game."""
    info = game_info(game)
    gid, platform = info["id"], info["platform"]
    compiler = compiler or (WII_DEFAULT if platform == "wii" else GC_DEFAULT)
    template = toolchains.ensure("dtk-template", log=log)
    shutil.copytree(template, root, ignore=shutil.ignore_patterns(".decompnet-toolchain.json"))
    for placeholder in ("orig/GAMEID", "config/GAMEID"):
        shutil.rmtree(root / placeholder, ignore_errors=True)
    orig, config = root / "orig" / gid, root / "config" / gid
    (orig / "sys").mkdir(parents=True)
    config.mkdir(parents=True)
    if info["kind"] == "dol":
        (orig / "sys" / "main.dol").write_bytes(info["dol"])
    else:
        try:
            (orig / game.name).hardlink_to(game)
        except OSError:
            shutil.copyfile(game, orig / game.name)
    sha1 = hashlib.sha1(info["dol"]).hexdigest()
    modules = disc_modules(game) if info["kind"] == "disc" else []
    if modules:
        log(f"{len(modules)} REL modules on the disc: analysed with the program")
    # Modules that are never loaded together may share an id (Mario Party's minigames all do): a reference to
    # "module 1" then can't be resolved, and each links against the program only, as the community's projects set it
    shared_ids = len({m[3] for m in modules}) < len(modules)
    (config / "config.yml").write_text(f"""# Made by decomp.wtf. See config.example.yml for every option.
object_base: orig/{gid}
object: sys/main.dol
hash: {sha1}
symbols: config/{gid}/symbols.txt
splits: config/{gid}/splits.txt
mw_comment_version: {comment_version(compiler)}
detect_objects: true
detect_strings: true
write_asm: true
""" + ("modules:\n" + "".join(f"""- object: {path}
  hash: {rel_sha1}
  symbols: config/{gid}/rels/{name}/symbols.txt
  splits: config/{gid}/rels/{name}/splits.txt
""" + ("  links: []\n" if shared_ids else "") for name, path, rel_sha1, _ in modules) if modules else ""), encoding="utf-8")
    for name, *_ in modules:
        (config / "rels" / name).mkdir(parents=True, exist_ok=True)
    (config / "build.sha1").write_text(f"{sha1}  build/{gid}/main.dol\n" + "".join(
        f"{rel_sha1}  build/{gid}/{name}/{name}.rel\n" for name, _, rel_sha1, _ in modules), encoding="utf-8")
    (root / "include").mkdir(exist_ok=True)
    (root / "include" / "common.h").write_text(TYPES_H, encoding="utf-8")
    _write_configure(root, gid, compiler, [], rels=bool(modules))

    fp = ForkProject(root)
    log(f"analysing {info['title']} ({gid}) with decomp-toolkit")
    ok, tail = _build(fp, log)
    if not ok and (renamed := _unique_sections(root, gid)):
        log(f"the program has sections of its own (named {', '.join(renamed)} here): analysing it again")
        ok, tail = _build(fp, log)
    if (named := _standard_module_sections(root, gid)):
        log(f"named module sections decomp-toolkit couldn't ({', '.join(named[:4])}{', ...' if len(named) > 4 else ''}): "
            "analysing again")
        ok, tail = _build(fp, log)
    if _name_entry(root, gid, info["dol"]):
        ok, tail = _build(fp, log)
    if not ok:
        raise ForkError(f"the first build didn't rebuild the game identically:\n{tail[-1500:]}")

    units = _code_units(root, gid)
    sources = []
    splits: dict[str | None, str] = {}
    for module, start, end, section in units:
        path = config / "splits.txt" if module is None else config / "rels" / module / "splits.txt"
        if module not in splits:
            splits[module] = path.read_text(encoding="utf-8").rstrip("\n") + "\n"
        name = f"auto/text_{start:08X}.c" if module is None else f"auto/{module}/text_{start:08X}.c"
        sources.append(name)
        splits[module] += f"\n{name}:\n\t{section:<11} start:0x{start:08X} end:0x{end:08X}\n"
        src = root / "src" / name
        src.parent.mkdir(parents=True, exist_ok=True)
        where = "" if module is None else f" of {module}"
        src.write_text(f'/* Functions from 0x{start:08X} to 0x{end:08X}{where}, found by decomp-toolkit. */\n'
                       f'#include "common.h"\n', encoding="utf-8")
    for module, text in splits.items():
        (config / "splits.txt" if module is None else config / "rels" / module / "splits.txt").write_text(text, encoding="utf-8")
    _write_configure(root, gid, compiler, sources, rels=bool(modules))
    log(f"{len(sources)} source files for agents to write into")
    ok, tail = _build(fp, log)
    if not ok:
        raise ForkError(f"the project didn't rebuild the game identically after adding source files:\n{tail[-1500:]}")

    (root / ".decomp.wtf.json").write_text(json.dumps({
        "created": "from scratch", "platform": platform, "version": gid, "compiler": compiler,
        "game": {"what": "your copy of the game", "path": f"orig/{gid}/" + ("sys/main.dol" if info["kind"] == "dol" else game.name),
                 "sha1": hashlib.sha1((orig / "sys" / "main.dol").read_bytes()).hexdigest() if info["kind"] == "dol" else None},
        "setup": [["python", "configure.py", *tool_args()], ["ninja"]],
    }, indent=2), encoding="utf-8")
    (root / "README.example.md").unlink(missing_ok=True)
    with open(root / ".gitignore", "a", encoding="utf-8") as f:
        f.write("\n# decomp.wtf\n.decomp-wtf/\n")
    (root / "README.md").write_text(f"""# {info['title']} ({gid}) decompilation

Started from scratch with [decomp.wtf](https://decomp.wtf) on [dtk-template](https://github.com/encounter/dtk-template),
the layout GameCube and Wii decomps use. Every build rebuilds `main.dol` and checks it against your copy, byte for byte.

1. Put your own copy of the game in `orig/{gid}/` (never commit it).
2. `python configure.py` (downloads the tools it needs), then `ninja`.
3. Decompiled code goes in `src/`; `objdiff` (objdiff.json) shows each function's progress.

Compiler: CodeWarrior {compiler} (change `config.linker_version` in configure.py if the game used another).
""", encoding="utf-8")
    if shutil.which("git"):
        for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                    ["git", "-c", "user.name=decomp.wtf", "-c", "user.email=noreply@decomp.wtf", "commit", "-q", "-m",
                     f"Start the {gid} decompilation (decomp.wtf, dtk-template)"]):
            subprocess.run(cmd, cwd=root, capture_output=True)
    return {"platform": platform, "id": gid, "title": info["title"], "compiler": compiler, "sources": len(sources)}
