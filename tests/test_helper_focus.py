"""The helpers' Focus menu (Quick Wins, Close Calls): a helper started with a focus claims by it, whatever strategy
it asks for, and stops when that focus has nothing left. Stub-based: no compiler needed."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from decompnet.app import agents, tools
from decompnet.app.engine import RomNetwork

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def net(tmp_path, monkeypatch):
    n = RomNetwork(tmp_path / "app.db", tmp_path)
    with n.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'fixture', 'fixture', 'focus fixture', 'mips', 0, ?, '{\"kind\": \"rom\"}')", (str(tmp_path),))
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, '', NULL, 'fixture', 0, '')")
        rows = [  # name, instructions, best score (None: never compiled)
            ("small_fn", 20, None),      # a Quick Win: small, untouched
            ("big_far_fn", 200, 15000),  # big, 75% off: nobody's focus
            ("close_fn", 100, 500),      # a Close Call: 95% matched
            ("mid_fn", 100, 5000),       # 50% off: default claiming only
        ]
        for i, (name, n_insns, best) in enumerate(rows, start=1):
            # a seed: the ROM brief drafts none itself (that needs a compiler config)
            db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, seed, status, "
                       "attempts, abandons, best_score, best_source) VALUES(?,1,?,?,?,?,'[]','nop','/* m2c */','open',0,0,?,?)",
                       (i, name, n_insns * 4, n_insns, b"\0" * (n_insns * 4), best, None if best is None else "int f(void);"))
    monkeypatch.setattr(n, "_extra_brief", lambda fn, proj: [])  # the ROM brief's map block needs a real import
    return n


def agent(net, name):
    return net.authenticate(net.register_agent(name))


def test_each_focus_counts_only_its_own_functions(net):
    assert net.strategy_left("fixture", "quick") == 1   # small_fn
    assert net.strategy_left("fixture", "close") == 1   # close_fn
    assert net.strategy_left("fixture", "easiest") == 4


def test_quick_wins_claims_the_small_one_and_close_calls_the_near_miss(net):
    alice, bob = agent(net, "alice"), agent(net, "bob")
    assert "small_fn" in net.claim(alice, "fixture", None, "quick").splitlines()[0]
    assert "close_fn" in net.claim(bob, "fixture", None, "close").splitlines()[0]
    # with those taken, each focus is out of work for a third helper, while default claiming still has plenty
    assert net.strategy_left("fixture", "quick") == 0 and net.strategy_left("fixture", "close") == 0
    assert net.strategy_left("fixture", "easiest") == 2
    # the helper that holds the function still counts it as its own work
    assert net.strategy_left("fixture", "quick", alice.id) == 1


def test_a_helpers_focus_overrides_the_strategy_an_agent_asks_for(net):
    carol = agent(net, "carol")
    net.focus[carol.id] = "close"
    out = tools._claim(net, carol, "fixture", "", "easiest", count=4)  # asks for a batch of the easiest
    assert "close_fn" in out.splitlines()[0], out
    assert "small_fn" not in out  # a Close Call never adds a batch of neighbours
    net.focus.pop(carol.id)
    dave = agent(net, "dave")
    assert "small_fn" in tools._claim(net, dave, "fixture", "", "easiest", count=1)  # no focus: as always


def test_focus_table_matches_the_menu_in_the_web_page():
    js = (ROOT / "src" / "decompnet" / "app" / "static" / "app.js").read_text(encoding="utf-8")
    menu = set(re.findall(r'\{ id: "(\w+)", name: "[^"]+"', js.split("const FOCUS = ", 1)[1].split("];", 1)[0]))
    assert menu == {"default", *agents.FOCUSES}
    for strategy, left_text, label in agents.FOCUSES.values():
        assert strategy in ("quick", "close") and left_text and label
