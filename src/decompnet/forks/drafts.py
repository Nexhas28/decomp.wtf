"""Machine-made first drafts for functions of a fork that are still assembly.

Community projects keep the assembly of undecompiled functions in the tree: splat projects one file per
function (`asm/nonmatchings/<file>/<function>.s`, referenced by `INCLUDE_ASM`), dtk projects one file per
unit under `build/<version>/asm/`. m2c turns that into a C draft for MIPS (N64, PS1, PS2), PowerPC
(GameCube, Wii), ARM and Thumb (GBA, DS).
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import seeds, toolchains

# platform -> m2c target, without the language
M2C = {"n64": "mips", "ps": "mipsel-gcc", "psp": "mipsel-gcc", "ps2": "mipsee-gcc", "gc": "ppc-mwcc",
       "wii": "ppc-mwcc", "gba": "gba", "nds": "arm", "3ds": "arm"}
MAX_ASM = 2 * 1024 * 1024


def supported(platform: str) -> bool:
    return platform in M2C


def find_asm(root: Path, unit_name: str, symbol: str, source_text: str | None) -> Path | None:
    """The assembly file holding `symbol`, if the project has one."""
    if source_text:
        m = re.search(rf'INCLUDE_ASM\(\s*"([^"]+)"\s*,\s*{re.escape(symbol)}\s*\)', source_text)
        if m and (root / m.group(1) / f"{symbol}.s").is_file():
            return root / m.group(1) / f"{symbol}.s"
        m = re.search(rf'GLOBAL_ASM\(\s*"([^"]*/{re.escape(symbol)}\.s)"\s*\)', source_text)
        if m and (root / m.group(1)).is_file():
            return root / m.group(1)
    for base in (root / "asm" / "nonmatchings", root / "asm"):
        if base.is_dir():
            for hit in base.rglob(f"{symbol}.s"):
                return hit
    # dtk: build/<version>/asm/<unit path>.s, unit names being "<module>/<path>"
    parts = unit_name.split("/")
    for asm_root in root.glob("build/*/asm"):
        for rel in ("/".join(parts), "/".join(parts[1:])):
            if rel and (asm_root / f"{rel}.s").is_file():
                return asm_root / f"{rel}.s"
    return None


def _block(text: str, symbol: str) -> str:
    """Just one function from a large dtk assembly file."""
    m = re.search(rf"^\.fn {re.escape(symbol)}\b.*?^\.endfn {re.escape(symbol)}\b.*?$", text, re.M | re.S)
    return m.group(0) if m else text


def draft(root: Path, platform: str, unit_name: str, symbol: str, source_path: str | None,
          compiler_hint: str = "", timeout: float = 30) -> str | None:
    target = M2C.get(platform)
    if not target or toolchains.installed("m2c") is None:
        return None
    src = root / source_path if source_path else None
    source_text = src.read_text(errors="replace", encoding="utf-8") if src and src.exists() else None
    asm = find_asm(root, unit_name, symbol, source_text)
    if asm is None:
        return None
    text = asm.read_text(errors="replace", encoding="utf-8")
    if len(text) > MAX_ASM or ".fn " in text:
        text = _block(text, symbol)
    if platform == "n64":
        target += "-ido" if "ido" in compiler_hint.lower() else "-gcc"
    lang = "c++" if source_path and Path(source_path).suffix.lower() in (".cpp", ".cc", ".cxx", ".cp") else "c"
    return seeds.run_m2c(text, symbol, f"{target}-{lang}", timeout=timeout)
