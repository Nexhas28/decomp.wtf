"""The guides tell agents what the code does: when compile_and_diff says to release a stalled function, and what
each diff marker means."""

import re

import pytest

from decompnet import scoring
from decompnet.app import tools
from decompnet.network import Network

from .test_acceptance import make_project

STALL_GUIDES = ["workflow", "fork", "similarity"]


def ladder(text: str) -> list[tuple[int, int]]:
    """(function size in instructions, compiles without a better score) pairs a guide states."""
    pattern = (r"after (\d+) compiles without a better score for functions under (\d+) instructions, "
               r"(\d+) under (\d+), (\d+) under (\d+) and (\d+) for bigger ones")
    m = re.search(pattern.replace(" ", r"\s+"), text)  # (lines wrap anywhere)
    assert m, "the guide no longer states when compile_and_diff suggests releasing"
    a, n1, b, n2, c, n3, d = map(int, m.groups())
    return [(n1 - 1, a), (n2 - 1, b), (n3 - 1, c), (n3, d)]


@pytest.mark.parametrize("topic", STALL_GUIDES)
def test_the_guides_state_the_release_nudge_the_code_gives(tmp_path, topic):
    text = tools._guide(topic)
    assert not re.search(r"~\s*\d+ compiles|\d+-\d+ compiles", text)  # the old rule-of-thumb counts
    for n_insns, after in ladder(text):
        net = Network(tmp_path / f"{topic}-{n_insns}.db")
        make_project(net, 1)
        with net.store.tx() as db:
            db.execute("UPDATE functions SET n_insns=? WHERE id=1", (n_insns,))
        agent = net.authenticate(net.register_agent("alice"))
        fn = net.store.one("SELECT * FROM functions WHERE id=1")
        net._record(fn, agent, 1000, "int f(void) { return 0; }", 1)  # the best so far
        for i in range(after - 1):
            net._record(fn, agent, 1000, f"int f(void) {{ return {i + 1}; }}", 1)
        assert net.stall_note(1, agent.id) == "", (topic, n_insns, after)
        net._record(fn, agent, 1000, "int f(void) { return -1; }", 1)
        assert "release_function" in net.stall_note(1, agent.id), (topic, n_insns, after)


def test_the_workflow_guide_explains_every_diff_marker():
    legend = tools._guide("workflow").split("## Reading the diff", 1)[1]
    for mark in scoring.MARKERS:
        assert f"`{mark}`" in legend, mark
