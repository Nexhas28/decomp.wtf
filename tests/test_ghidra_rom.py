"""The ROM-evidence lane (PR-31): pure-python MIPS ELF wrapping of the project's segment images and
a headless Ghidra decompile of one target function. Read-only evidence; no project-state mutation."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from decompnet.forks import ghidra


def _proj(tmp_path: Path, rom: bytes, segments: list[dict]) -> dict:
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    (root / "game.rom").write_bytes(rom)
    return {"id": 1, "slug": "fixture", "root": str(root),
            "config": json.dumps({"kind": "rom", "big_endian": False,
                                  "rom": {"file": "game.rom"}, "segments": segments})}


def test_rom_wrapper_is_a_valid_little_endian_mips_elf(tmp_path):
    rom = bytes(range(256)) * 2
    proj = _proj(tmp_path, rom, [{"rom_start": 0, "rom_end": 512, "vram": 0x80010000}])
    elf = ghidra.rom_wrapper(proj, tmp_path / "cache")
    data = elf.read_bytes()
    assert data[:4] == b"\x7fELF" and data[5] == 1  # 32-bit little-endian
    e_machine, e_phnum = struct.unpack_from("<H", data, 18)[0], struct.unpack_from("<H", data, 44)[0]
    assert e_machine == 8 and e_phnum == 1  # MIPS, one segment
    p_offset = struct.unpack_from("<I", data, 52 + 4)[0]
    p_vaddr = struct.unpack_from("<I", data, 52 + 8)[0]
    p_filesz = struct.unpack_from("<I", data, 52 + 16)[0]
    assert p_offset == 0x1000 and p_vaddr == 0x80010000 and p_filesz == 512
    # the payload at the file offset is the rom bytes verbatim
    assert data[p_offset:p_offset + 8] == rom[:8]
    # cached: a second call returns the same file without rewriting
    assert ghidra.rom_wrapper(proj, tmp_path / "cache") == elf


def test_big_endian_roms_are_not_admitted(tmp_path):
    proj = _proj(tmp_path, b"\x00" * 64, [{"rom_start": 0, "rom_end": 64, "vram": 0x80010000}])
    cfg = json.loads(proj["config"])
    cfg["big_endian"] = True
    proj["config"] = json.dumps(cfg)
    with pytest.raises(ghidra.AdmissionError, match="little-endian"):
        ghidra.rom_wrapper(proj, tmp_path / "cache")


def test_decompile_rom_function_real_lane(tmp_path):
    """Real lane: decompiles a tiny hand-crafted MIPS function through the pinned Ghidra. Skipped
    with an explicit reason when Ghidra or Java 21 is absent."""
    if not ghidra.available():
        pytest.skip("Ghidra (analyzeHeadless) or Java 21 is not installed on this host")
    # a realistic register-initializer: two lui/sb store pairs, then jr;nop
    #   addiu v0,0x80 ; lui at,0x8003 ; sb v0,0x101(at)
    #   addiu v0,3    ; lui at,0x8003 ; sb v0,0x105(at)
    #   jr ra ; nop
    import struct as _s
    code = b"".join(_s.pack("<I", w) for w in [0x24020080, 0x3C018003, 0xA0220101,
                                               0x24020003, 0x3C018003, 0xA0220105,
                                               0x03E00008, 0x00000000])
    rom = bytearray(0x2000)
    rom[0x1000:0x1020] = code
    proj = _proj(tmp_path, bytes(rom), [{"rom_start": 0, "rom_end": len(rom), "vram": 0x80010000}])
    res = ghidra.decompile_rom_function(proj, 0x80011000, len(code), tmp_path / "cache", timeout=240)
    assert res["status"] == "ok", res["diagnostics"]
    assert "0x80" in (res["c"] or "") or "128" in (res["c"] or "")
