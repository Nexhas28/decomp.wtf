"""N64 code outside the main segment (rom/n64code.py): found, and placed at the address it runs from, from its own
calls. Super Mario 64's engine (0x80378800) and goddard (0x8016F000) segments are found this way; here a synthetic
ROM stands in for a game."""

import struct

from decompnet.rom.n64code import find_segments

NOP, JR_RA = 0, 0x03E00008


def _function(vram: int, callee: int) -> list[int]:
    """addiu sp,-0x18; sw ra; jal callee; nop; lw ra; addiu sp,0x18; jr ra; nop"""
    return [0x27BDFFE8, 0xAFBF0014, 0x0C000000 | ((callee >> 2) & 0x03FFFFFF), NOP, 0x8FBF0014, 0x27BD0018, JR_RA, NOP]


def _code(vram: int, count: int, calls_into: int | None = None) -> bytes:
    words = []
    for i in range(count):
        here = vram + i * 32
        callee = vram + ((i + 1) % count) * 32 if calls_into is None or i % 2 else calls_into + (i % 7) * 32
        words += _function(here, callee)
    return struct.pack(f">{len(words)}I", *words)


def test_a_segment_loaded_later_is_found_at_its_address():
    rom = bytearray(0x200000)
    main = _code(0x80000400, 64, calls_into=0x80300000)  # the main code calls into the other segment
    rom[0x1000:0x1000 + len(main)] = main
    extra = _code(0x80300000, 120)
    rom[0x100000:0x100000 + len(extra)] = extra
    rom[0x180000:0x180400] = bytes(range(256)) * 4  # data: no code in it
    found = find_segments(bytes(rom), [(0x1000, 0x1000 + len(main), 0x80000400)])
    assert found == [(0x100000, 0x100000 + len(extra), 0x80300000)]


def test_code_at_an_address_already_taken_is_left_out():
    rom = bytearray(0x200000)
    main = _code(0x80300000, 64)
    rom[0x1000:0x1000 + len(main)] = main
    overlay = _code(0x80300000, 120)  # an overlay loaded where other code runs: addresses would clash
    rom[0x100000:0x100000 + len(overlay)] = overlay
    assert find_segments(bytes(rom), [(0x1000, 0x1000 + len(main), 0x80300000)]) == []


def test_nothing_but_data_finds_nothing():
    rom = bytes(range(256)) * 0x2000
    assert find_segments(rom, [(0x1000, 0x2000, 0x80000400)]) == []
