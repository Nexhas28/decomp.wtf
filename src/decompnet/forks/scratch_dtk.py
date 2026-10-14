"""Start a GameCube or Wii decompilation from scratch: a dtk-template project made for the user's own game.

dtk-template is the community's standard project layout (CC0). decomp-toolkit analyses the game's program, finds
every function and splits the program into objects; the project then rebuilds the program byte for byte (checked
against its SHA-1) on every build, with decompiled source linked in as units match. That makes the result the base
for ports and mods from the first minute, and the same kind of project as the GameCube and Wii decomps on
decomp.dev, so it can be shared and continued anywhere.

Each code region dtk finds gets a source file (src/auto/text_<address>.c) for agents to write into; decomp.wtf's
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


def _libs_block(compiler: str, sources: list[str]) -> str:
    objects = "".join(f'            Object(NonMatching, "{s}"),\n' for s in sources)
    return f'''config.libs = [
    {{
        "lib": "game",
        "mw_version": config.linker_version,
        "cflags": cflags_base,
        "progress_category": "game",
        "objects": [
{objects}        ],
    }},
]


'''


def _write_configure(root: Path, game_id: str, compiler: str, sources: list[str]) -> None:
    p = root / "configure.py"
    s = p.read_text(encoding="utf-8")
    s = re.sub(r'VERSIONS = \[\n.*?\n\]', f'VERSIONS = [\n    "{game_id}",  # 0\n]', s, count=1, flags=re.S)
    s = re.sub(r'^config\.linker_version = ".*"$', f'config.linker_version = "{compiler}"', s, count=1, flags=re.M)
    if "config.shift_jis = False" not in s:  # sources are plain C (turn it back on for Japanese string literals)
        s = s.replace(f'config.linker_version = "{compiler}"', f'config.linker_version = "{compiler}"\nconfig.shift_jis = False', 1)
    start = s.index("config.libs = [")
    end = s.index("# Optional callback to adjust link order")
    p.write_text(s[:start] + _libs_block(compiler, sources) + s[end:], encoding="utf-8")


def _build(fp: ForkProject, log) -> tuple[bool, str]:
    ok, tail = fp.run([sys.executable, "configure.py", *tool_args()], log=log)
    if not ok:
        return False, tail
    return fp.run(["ninja"], log=log, timeout=3600)


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


def _code_units(root: Path, game_id: str) -> list[tuple[int, int, str]]:
    """(start, end, section) of the code regions dtk split out automatically (not .init: hand-written start-up)."""
    from elftools.elf.elffile import ELFFile

    cfg = json.loads((root / "build" / game_id / "config.json").read_text(encoding="utf-8"))
    out = []
    for unit in (cfg.get("units") or []):
        m = re.match(r"auto_\d+_([0-9A-F]{8})_(\w+)$", unit.get("name", ""))
        if not m or m.group(2) != "text":
            continue
        with open(root / unit["object"], "rb") as f:
            elf = ELFFile(f)
            sec = elf.get_section_by_name(".text")
            size = sec["sh_size"] if sec is not None else 0
        if size:
            start = int(m.group(1), 16)
            out.append((start, start + size, ".text"))
    return sorted(out)


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
""", encoding="utf-8")
    (config / "build.sha1").write_text(f"{sha1}  build/{gid}/main.dol\n", encoding="utf-8")
    (root / "include").mkdir(exist_ok=True)
    (root / "include" / "common.h").write_text(TYPES_H, encoding="utf-8")
    _write_configure(root, gid, compiler, [])

    fp = ForkProject(root)
    log(f"analysing {info['title']} ({gid}) with decomp-toolkit")
    ok, tail = _build(fp, log)
    if _name_entry(root, gid, info["dol"]):
        ok, tail = _build(fp, log)
    if not ok:
        raise ForkError(f"the first build didn't rebuild the game identically:\n{tail[-1500:]}")

    units = _code_units(root, gid)
    sources = []
    splits = (config / "splits.txt").read_text(encoding="utf-8").rstrip("\n") + "\n"
    for start, end, section in units:
        name = f"auto/text_{start:08X}.c"
        sources.append(name)
        splits += f"\n{name}:\n\t{section:<11} start:0x{start:08X} end:0x{end:08X}\n"
        src = root / "src" / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(f'/* Functions from 0x{start:08X} to 0x{end:08X}, found by decomp-toolkit. */\n'
                       f'#include "common.h"\n', encoding="utf-8")
    (config / "splits.txt").write_text(splits, encoding="utf-8")
    _write_configure(root, gid, compiler, sources)
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
