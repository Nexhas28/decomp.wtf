"""Code an N64 game loads later: segments outside the main one that splat's create_config finds.

splat finds the boot code and the main code segment. Many games keep more code elsewhere in the ROM and load it at
run time to an address of its own: Super Mario 64's engine (0x80378800) and Mario's head on the title screen
("goddard", 0x8016F000) are a third of its code. Nothing in the ROM says where such code goes, but the code itself
does: its calls (jal) are absolute addresses, and they land on its own functions' starts. So:

1. find stretches of the ROM that look like MIPS code (functions returning with `jr $ra`, stack frames made with
   `addiu $sp, $sp, -N`);
2. for each, the load address that makes most of its calls land on stack-frame starts in it;
3. its first function: the lowest address anything calls in it (the main code calls into it too), its end: the
   last return.

Segments whose addresses overlap code already known (overlays sharing one address, as Zelda's actors do) are left
out: function addresses stay unique in a project.
"""

from __future__ import annotations

import collections
import struct

JR_RA = 0x03E00008
BLOCK = 0x400
KSEG0 = (0x80000000, 0x80800000)


def _frame(word: int) -> bool:
    return (word & 0xFFFF8000) == 0x27BD8000  # addiu $sp, $sp, -N


def _jal_targets(words: tuple[int, ...]) -> list[int]:
    return [((w & 0x03FFFFFF) << 2) | 0x80000000 for w in words if (w >> 26) == 3]


def _code_like(words: tuple[int, ...]) -> bool:
    """A return or a stack frame: either is rare in anything but code (a large function can go a while without the
    other)."""
    return any(w == JR_RA or _frame(w) for w in words)


def find_segments(rom: bytes, taken: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """(rom_start, rom_end, vram) of code segments outside `taken` (rom_start, rom_end, vram of the known ones)."""
    size = len(rom) & ~3
    known_code = [(s, e) for s, e, _ in taken]
    calls: list[int] = []  # every call the known code makes: some go into the code found here
    for s, e in known_code:
        calls += _jal_targets(struct.unpack(f">{(e - s) // 4}I", rom[s:e - (e - s) % 4]))
    gaps, pos = [], 0x1000
    for s, e in sorted(known_code):
        if s > pos:
            gaps.append((pos, s))
        pos = max(pos, e)
    if pos < size:
        gaps.append((pos, size))

    found = []
    used_vram = [(v, v + (e - s)) for s, e, v in taken]
    for g_lo, g_hi in gaps:
        regions, cur = [], None
        for off in range(g_lo, g_hi - 0x20, BLOCK):
            end = min(off + BLOCK, g_hi) & ~3
            if _code_like(struct.unpack(f">{(end - off) // 4}I", rom[off:end])):
                if cur and off <= cur[1] + 2 * BLOCK:  # a quiet stretch inside a segment is fine
                    cur[1] = end
                else:
                    cur = [off, end]
                    regions.append(cur)
        floor = g_lo
        for lo, hi in regions:
            seg = _place(rom, lo, hi, floor, calls)
            if seg and not any(a < seg[2] + (seg[1] - seg[0]) and seg[2] < b for a, b in used_vram):
                found.append(seg)
                used_vram.append((seg[2], seg[2] + seg[1] - seg[0]))
                floor = seg[1]
    return sorted(found)


def _place(rom: bytes, lo: int, hi: int, floor: int, calls: list[int]) -> tuple[int, int, int] | None:
    words = struct.unpack(f">{(hi - lo) // 4}I", rom[lo:hi])
    frames = [i * 4 for i, w in enumerate(words) if _frame(w)]
    returns = [i * 4 for i, w in enumerate(words) if w == JR_RA]
    own = set(_jal_targets(words))
    if len(frames) < 8 or len(returns) < 8 or not own:
        return None
    votes: collections.Counter = collections.Counter()
    for t in own:
        for f in frames:
            base = t - f
            if KSEG0[0] <= base < KSEG0[1] and base % 4 == 0:
                votes[base] += 1
    if not votes:
        return None
    base, hits = votes.most_common(1)[0]  # the address ROM `lo` is loaded at
    if hits < max(8, len(frames) // 10):
        return None
    # the first function: the lowest address called in it (from itself or the known code). Every function returns,
    # so code before `lo` is at most a little: a stray "call" from data words can't drag the start further back.
    back = max(floor, lo - 2 * BLOCK) - lo
    called = [t - base for t in own | set(calls) if back <= t - base < hi - lo]
    first = min([frames[0], *called])
    start = lo + first
    end = lo + returns[-1] + 8  # the last return and its delay slot
    end = min((end + 0xF) & ~0xF, len(rom))
    return (start, end, base + first)
