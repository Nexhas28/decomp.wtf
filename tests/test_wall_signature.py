"""A wall is prose, not a path.

A release note that merely named a tool directory ("left for azflash-4/gb-merge-fragments") was filed as a
`boundary-fragment` wall for six hours, because `fragment` matched inside the path. Wall words must match
as words: the lookbehind refuses a word preceded by a word character or a hyphen.
"""
from __future__ import annotations

import pytest

from decompnet.network import wall_signature

NOT_A_WALL = [
    "left for azflash-4/gb-merge-fragments to pick up",
    "tools/azflash-4/gb-split-blobs found nothing",
    "see tools/azflash-2/ceiling/usage.md",
    "ordinary handoff: released for another agent",
]


@pytest.mark.parametrize("text", NOT_A_WALL)
def test_paths_and_plain_handoff_are_not_walls(text):
    assert wall_signature(text) is None


@pytest.mark.parametrize("text,kind", [
    ("this row is a boundary fragment of the previous routine", "boundary-fragment"),
    ("a false split by mgbdis", "boundary-fragment"),
    ("the continuation of Call_000_1234", "boundary-fragment"),
    ("hit the toolchain floor here", "toolchain-floor"),
    ("walled by the compiler generation (toolchain)", "toolchain-floor"),
    ("register allocation tie on the saved set", "regalloc-tie"),
    ("hand-written assembly; MIPS-shaped", "bios-veneer"),
    ("a jump table mgbdis misread", "jump-table"),
])
def test_prose_still_names_its_wall(text, kind):
    assert wall_signature(text) == kind
