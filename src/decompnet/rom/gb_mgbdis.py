"""Game Boy split by mgbdis + rgbds: real code/data separation, and verifiable.

The built-in split (`gb.functions`) walks the ROM with the decoder and cuts functions at call targets.
That is a guess. [mgbdis](https://github.com/mattcurrie/mgbdis) does a proper sweep — reachability,
jump tables, code/data separation, RGBDS labels — and its output, assembled by
[rgbds](https://github.com/gbdev/rgbds), **rebuilds the ROM byte for byte**. That reassembly check is
what makes this split trustworthy: if the disassembly did not cover the ROM exactly, the rebuild would
differ, and we report that instead of pretending.

Pipeline (all in a temp dir):

    mgbdis.py <rom> --output-dir out        # out/bank_XXX.asm + game.asm + Makefile
    rgbasm -o game.o game.asm               # assemble every bank
    rgblink -n game.sym -o game.gbc game.o  # link, writing the symbol file
    rgbfix -v -p 255 game.gbc               # the padding a released cartridge has
    cmp game.gbc <rom>                      # faithful? (reported, and required for confidence)

`game.sym` is `bank:address Name` per line; those labels are the function starts, with the bytes read
from the original ROM (the rebuild is only the proof, never the source of truth).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import gb, sm83
from .n64 import RomError

SYM = re.compile(r"^([0-9A-Fa-f]{2}):([0-9A-Fa-f]{4})\s+(\S+)")
LABEL_LINE = re.compile(r"^([A-Za-z_.][\w.$]*?)::?\s*$")
#: What a **function entry** looks like in mgbdis's output: something a `call` reaches (`Call_`/`Func_`),
#: a reset vector (`RST_`) or an interrupt vector. `Jump_` and `jr_` labels are *internal branch targets*
#: — mgbdis emits tens of thousands of them (26,709 `jr_` and 1,511 `Jump_` on a 1 MiB cartridge) and
#: treating them as boundaries cuts functions into one-byte pieces. Data labels are excluded too.
FUNCTION_PREFIXES = ("Func_", "Call_", "RST_")
INTERRUPT_SUFFIX = "Interrupt"
DATA_PREFIXES = ("Data_", "Unknown_", "Gfx_", "Pal_", "Map_", "Text_", "String_", "Ptr_", "Table_",
                 "Tileset_", "Tilemap_", "Music_", "SFX_", "Obj_", "Header")
MAX_FUNCTION = 0x400


@dataclass
class ToolSet:
    python: str
    mgbdis: Path
    rgbasm: Path
    rgblink: Path
    rgbfix: Path


def _tool(tid: str, name: str) -> Path | None:
    """A binary from an installed toolchain (any layout: the fork's installer, a nested archive, or a
    system install) — the same discovery rule gb_build uses for GBDK."""
    try:
        from .. import toolchains as tc

        roots = [Path(tc.path(tid))]
    except Exception:
        roots = []
    roots += [Path.home() / "Library/Application Support/decompnet/toolchains" / tid,
              Path.home() / ".local/share/decompnet/toolchains" / tid]
    for root in roots:
        if not root.is_dir():
            continue
        for cand in [root] + sorted(p for p in root.iterdir() if p.is_dir()):
            for sub in (cand / "bin", cand):
                exe = sub / name
                if exe.exists():
                    return exe
    return None


def tools() -> ToolSet | None:
    """mgbdis + rgbds, or None when they are not installed (the caller falls back to the built-in split)."""
    import sys

    # The released source archive nests itself (mgbdis-3.0/mgbdis.py), a checkout may not: accept both.
    roots: list[Path] = []
    try:
        from .. import toolchains as tc

        roots.append(Path(tc.path("mgbdis")))
    except Exception:
        pass
    roots += [Path.home() / "Library/Application Support/decompnet/toolchains/mgbdis",
              Path.home() / ".local/share/decompnet/toolchains/mgbdis"]
    mgbdis_dir = None
    for root in roots:
        if not root.is_dir():
            continue
        for cand in [root] + sorted(p for p in root.iterdir() if p.is_dir()):
            if (cand / "mgbdis.py").exists():
                mgbdis_dir = cand
                break
        if mgbdis_dir:
            break
    rgbasm, rgblink = _tool("rgbds", "rgbasm"), _tool("rgbds", "rgblink")
    if mgbdis_dir is None or rgbasm is None or rgblink is None:
        # a system rgbds (Homebrew, a distro package) is fine too
        rgbasm = rgbasm or (Path(p) if (p := shutil.which("rgbasm")) else None)
        rgblink = rgblink or (Path(p) if (p := shutil.which("rgblink")) else None)
        if mgbdis_dir is None or rgbasm is None or rgblink is None:
            return None
    rgbfix = _tool("rgbds", "rgbfix") or (Path(p) if (p := shutil.which("rgbfix")) else None)
    return ToolSet(python=sys.executable, mgbdis=mgbdis_dir / "mgbdis.py", rgbasm=rgbasm,
                   rgblink=rgblink, rgbfix=rgbfix or rgblink.with_name("rgbfix"))


def available() -> bool:
    return tools() is not None


def _run(argv: list, cwd: Path, timeout: float = 900.0) -> tuple[int, str]:
    try:
        res = subprocess.run([str(a) for a in argv], cwd=str(cwd), capture_output=True, text=True,
                             timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    except FileNotFoundError as e:
        return 127, str(e)
    return res.returncode, (res.stdout or "") + (res.stderr or "")


# ------------------------------------------------------------------ pure parsing helpers

def parse_sym(text: str) -> list[tuple[int, int, str]]:
    """(bank, address, name) for every label in an rgblink symbol file (its header comment is skipped)."""
    out = []
    for line in text.splitlines():
        m = SYM.match(line.strip())
        if m:
            out.append((int(m.group(1), 16), int(m.group(2), 16), m.group(3)))
    return out


def is_code(name: str) -> bool:
    """Whether a symbol is a **function entry** (so it bounds a function), rather than an internal branch
    target, a local label, or data."""
    if "." in name:  # a local label inside another function
        return False
    if name.startswith(DATA_PREFIXES):
        return False
    if name.startswith(FUNCTION_PREFIXES):
        return True
    return name.endswith(INTERRUPT_SUFFIX) or name in ("EntryPoint", "Start", "Main")


def blocks(text: str) -> dict[str, str]:
    """{label: its assembly lines} out of one mgbdis bank file (RGBDS syntax, kept for the brief)."""
    out: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        m = LABEL_LINE.match(line)
        if m:
            if current is not None:
                out[current] = "\n".join(body).strip("\n")
            current, body = m.group(1), []
            continue
        if current is not None and line.strip() and not line.startswith(("SECTION", "INCLUDE", ";")):
            body.append(line.rstrip())
    if current is not None:
        out[current] = "\n".join(body).strip("\n")
    return out


def _trim_padding(code: bytes) -> bytes:
    """Drop a run of 0x00/0xFF padding at the end: labels cover data regions too, and padding is not
    part of the function."""
    end = len(code)
    while end and code[end - 1] in (0x00, 0xFF):
        end -= 1
    return code[:end] if end else code


# ------------------------------------------------------------------ the split

def split(rom_path: Path, workdir: Path, log=lambda *_: None) -> tuple[list[gb.GbFunction], dict]:
    """Functions from mgbdis's split, with the reassembly check reported in the second element.

    Raises RomError when the tools are missing or the disassembler/assembler fails. The returned dict is
    `{"faithful": bool, "labels": n, "functions": n, "rebuilt_bytes": n}`."""
    ts = tools()
    if ts is None:
        raise RomError("mgbdis and rgbds are not installed (see `decomp setup`)")
    rom_path = Path(rom_path)
    data = rom_path.read_bytes()
    if not gb.is_gb(data):
        raise RomError("this isn't a Game Boy or Game Boy Color ROM")
    with tempfile.TemporaryDirectory(prefix="gb-mgbdis-") as tmp:
        out = Path(tmp) / "out"
        # No `-I`: mgbdis imports its sibling modules from its own directory (the script dir must stay on
        # sys.path), and running it with the script path does exactly that.
        rc, log_text = _run([ts.python, ts.mgbdis, rom_path, "--output-dir", out], Path(tmp))
        if rc != 0 or not (out / "game.asm").exists():
            raise RomError(f"mgbdis could not disassemble the ROM: {log_text.strip()[-400:]}")
        log(f"mgbdis: disassembled {len(list(out.glob('bank_*.asm')))} banks")
        rc, log_text = _run([ts.rgbasm, "-o", "game.o", "game.asm"], out)
        if rc != 0 or not (out / "game.o").exists():
            raise RomError(f"rgbasm could not assemble the disassembly: {log_text.strip()[-400:]}")
        rc, log_text = _run([ts.rgblink, "-n", "game.sym", "-o", "game.gbc", "game.o"], out)
        if rc != 0 or not (out / "game.sym").exists():
            raise RomError(f"rgblink could not link the disassembly: {log_text.strip()[-400:]}")
        if ts.rgbfix.exists():
            _run([ts.rgbfix, "-v", "-p", "255", "game.gbc"], out)
        rebuilt = (out / "game.gbc").read_bytes() if (out / "game.gbc").exists() else b""
        faithful = rebuilt == data
        symbols = parse_sym((out / "game.sym").read_text(encoding="utf-8", errors="replace"))
        log(f"mgbdis: {len(symbols)} labels, reassembly {'byte-identical' if faithful else 'DIFFERS'}")

        # asm blocks, per bank file, so the brief can show the real disassembly
        asm_blocks: dict[str, str] = {}
        for f in sorted(out.glob("bank_*.asm")):
            asm_blocks.update(blocks(f.read_text(encoding="utf-8", errors="replace")))

        by_bank: dict[int, list[tuple[int, str]]] = {}
        for bank, addr, name in symbols:
            if is_code(name):
                by_bank.setdefault(bank, []).append((addr, name))
        for bank, items in by_bank.items():  # one entry per address: prefer the call-reached name
            best: dict[int, str] = {}
            for addr, name in items:
                current = best.get(addr)
                if current is None or (name.startswith(("Func_", "Call_")) and not current.startswith(("Func_", "Call_"))):
                    best[addr] = name
            by_bank[bank] = sorted(best.items())

        fns: list[gb.GbFunction] = []
        for bank, items in sorted(by_bank.items()):
            items.sort()
            for i, (addr, name) in enumerate(items):
                nxt = items[i + 1][0] if i + 1 < len(items) else (0x8000 if bank else 0x4000)
                flat = gb.flat_offset(addr, bank)
                if flat >= len(data):
                    continue
                end = min(flat + min(max(1, nxt - addr), MAX_FUNCTION), len(data))
                code = _trim_padding(data[flat:end])
                if not code:
                    continue
                fns.append(gb.GbFunction(name=name, vram=addr, rom=flat, bank=bank, code=code,
                                         n_insns=sum(1 for _ in sm83.disassemble(code, addr)),
                                         asm=asm_blocks.get(name, ""),
                                         refs=gb.refs_of(code, addr, bank, len(data))))
        meta = {"faithful": faithful, "labels": len(symbols), "functions": len(fns),
                "rebuilt_bytes": len(rebuilt), "original_bytes": len(data)}
        return fns, meta
