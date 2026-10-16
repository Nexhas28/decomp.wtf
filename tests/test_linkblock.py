"""Functions that keep failing at the link step are flagged as blocked on their symbols, told so in the brief,
and handed out last (linkblock.py)."""

from decompnet import linkblock

PROBLEMS = ["`gSaveBuf` is not a known symbol. Use the name the assembly shows (like D_80012340 or func_80010000)",
            "your data for D_800A1234 differs from the ROM: yours 00 00 00 01, the ROM's 00 00 00 02"]


def test_names_are_pulled_from_the_problems():
    assert linkblock.names(PROBLEMS) == ["gSaveBuf", "D_80012340", "func_80010000", "D_800A1234"]


def test_one_agent_alone_never_blocks_a_function():
    meta: dict = {}
    for _ in range(5):
        meta = linkblock.update(meta, PROBLEMS, agent_id=1)
    assert meta["link_problems"]["n"] == 5 and not linkblock.blocked(meta)
    assert linkblock.brief_lines(meta) == [] and linkblock.summary(meta) is None


def test_several_attempts_by_two_agents_block_it_and_the_brief_says_so():
    meta: dict = {}
    meta = linkblock.update(meta, PROBLEMS, agent_id=1)
    meta = linkblock.update(meta, PROBLEMS[:1], agent_id=1)
    assert not linkblock.blocked(meta)
    meta = linkblock.update(meta, PROBLEMS[1:], agent_id=2)
    assert linkblock.blocked(meta) and meta["link_problems"]["agents"] == [1, 2]
    text = "\n".join(linkblock.brief_lines(meta))
    assert "Needs symbols first" in text and "read_data" in text and "differs from the ROM" in text
    assert "gSaveBuf" in (linkblock.summary(meta) or "")


def test_a_compile_that_links_clears_the_record():
    meta = linkblock.update({}, PROBLEMS, 1)
    meta = linkblock.update(meta, PROBLEMS, 2)
    meta = linkblock.update(meta, PROBLEMS, 2)
    assert linkblock.blocked(meta)
    meta = linkblock.update(meta, [], 3)
    assert "link_problems" not in meta and not linkblock.blocked(meta)


def test_other_meta_is_kept():
    meta = linkblock.update({"bios": None, "for_agents": True}, PROBLEMS, 1)
    assert meta["for_agents"] is True and "link_problems" in meta
