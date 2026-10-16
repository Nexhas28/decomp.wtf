"""A Game Boy function does not end at a *conditional* return or jump.

`parse_listing()` builds each candidate function from `sdasgb -l`. It used to flush on
`^\\s*(reti?|jp\\s+hl|jr\\s)`, and `reti?` also matches `ret C` / `ret NC` / `ret Z` / `ret NZ` while bare
`jr` matches every conditional jump. So a compiled candidate was truncated at its first branch:

    u8 t1(u8 x) { if (x) return 1; return 2; }   ->  b7 3e 01 c0        (4 bytes, cut at `ret NZ`)
    the same function, fully parsed               ->  b7 3e 01 c0 3e 02 c9

Every ROM function that continues past its first conditional branch was therefore unmatchable — measured
on azuredreamsaaze, 1651 of 2631 targets. The fix flushes only on an UNCONDITIONAL transfer (`ret`,
`reti`, `jp hl`, or a `jr`/`jp` with no condition) or at the next label.
"""

from __future__ import annotations

import pytest

from decompnet.rom import gb_build

needs_toolchain = pytest.mark.skipif(not gb_build.available(),
                                     reason="the Game Boy C toolchain (gbdk-2020) is not installed")

SOURCE = """
typedef unsigned char u8;
u8 two(u8 x) { if (x) return 1; return 2; }
u8 jumps(u8 x) { if (x == 1) return 9; return 3; }
u8 loops(u8 x) { while (x) x--; return x; }
"""


@needs_toolchain
def test_conditional_return_does_not_end_the_function():
    funcs = gb_build.build_source(SOURCE, symbols={})
    code = funcs["two"].code
    assert len(code) >= 7, f"truncated at the first conditional return: {code.hex()}"
    assert code[-1] == 0xC9, "the function's final unconditional ret is missing"


@needs_toolchain
def test_conditional_jump_does_not_end_the_function():
    """`loops` compiles to `or a,a; ret Z; dec a; jr 00101$`: the function's exit is the conditional
    return and it ENDS on a backward jump, so the old rule truncated it at `ret Z` and lost the loop."""
    funcs = gb_build.build_source(SOURCE, symbols={})
    code = funcs["loops"].code
    assert len(code) >= 5, f"truncated at the conditional return: {code.hex()}"
    assert code[-2] == 0x18, f"the loop's final jr is missing: {code.hex()}"


@needs_toolchain
def test_every_function_in_one_source_is_still_separate():
    funcs = gb_build.build_source(SOURCE, symbols={})
    assert {"two", "jumps", "loops"} <= set(funcs), f"functions merged or lost: {sorted(funcs)}"
