"""Rooms and set-aside functions: BIOS calls and hand-written assembly stay out of every helper's way, the host's
and its members', while a function a person gave back to agents can still be had."""

import json

import pytest

from decompnet.network import NetworkError

from .test_bios import PRINTF
from .test_collab import pair  # noqa: F401  (a host and a member's copy of a small PS1 project, linked)


def _stub(net, status="asm"):
    """func_80010000 as a BIOS printf stub, the smallest function, so the easy strategies reach it first."""
    meta = {"bios": "PS1 BIOS call A0:3F (printf) wrapper: there's no game code in it to decompile"}
    with net.store.tx() as db:
        db.execute("UPDATE functions SET n_insns=1, target=?, status=?, meta=? WHERE name='func_80010000'",
                   (PRINTF, status, json.dumps(meta) if status == "asm" else None))


def _host_claims(host, name):
    return [r["detail"] for r in host.store.all("SELECT detail FROM events WHERE kind IN ('claim','release') "
                                                "AND detail=?", name)]


def _status(net, name="func_80010000"):
    return net.store.one("SELECT status FROM functions WHERE name=?", name)["status"]


@pytest.fixture
def room(pair, monkeypatch):  # noqa: F811
    host, guest, link, client, code = pair
    monkeypatch.setattr(guest, "_brief", lambda fn, agent, header="": f"{header} {fn['name']}")  # no asm here
    return host, guest, link, guest.local_agent("claude-1")[0]


def test_the_host_leaves_out_what_the_member_set_aside(room):
    """SLUS-01363: the host's copy still had the BIOS stubs open, every member's had them set aside, so each new
    helper was offered them, gave them back and was offered the next one."""
    host, guest, link, agent = room
    with host.store.tx() as db:
        db.execute("UPDATE functions SET n_insns=1 WHERE name='func_80010000'")  # open there, smallest first
    _stub(guest)
    assert guest.claim(agent, "test").split() == ["CLAIMED", "shop_open"]
    assert _host_claims(host, "func_80010000") == []  # never offered, so never given back


def test_an_older_host_that_ignores_the_list_is_asked_by_name(room, monkeypatch):
    host, guest, link, agent = room
    _stub(guest)
    asked = []

    def old_host(agent, function, strategy, skip=()):  # knows nothing of skip: offers the stub to a strategy claim
        asked.append(function)
        return function or "func_80010000"
    monkeypatch.setattr(link, "claim", old_host)
    monkeypatch.setattr(link, "call", lambda route, agent, /, **data: "")
    monkeypatch.setattr(link, "sync", lambda: None)
    assert guest.claim(agent, "test").split() == ["CLAIMED", "shop_open"]
    assert asked == [None, "shop_open"]  # one offer given back, then picked here and asked for by name


def test_the_host_never_hands_out_its_own_set_asides(room):
    host, guest, link, agent = room
    _stub(host)
    assert guest.claim(agent, "test").split() == ["CLAIMED", "shop_open"]
    other = guest.local_agent("claude-2")[0]
    assert guest.claim(other, "test").startswith("Nothing left to claim")
    with pytest.raises(NetworkError, match="func_80010000 is BIOS code, a PS1 BIOS call A0:3F"):
        guest.claim(other, "test", "func_80010000")


def test_a_by_name_claim_this_copy_set_aside_is_refused_clearly(room):
    host, guest, link, agent = room
    _stub(guest)  # open on the host, set aside here
    with pytest.raises(NetworkError, match="func_80010000 is BIOS code"):
        guest.claim(agent, "test", "func_80010000")
    assert host.store.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0  # given straight back on the host


def test_a_give_back_on_the_host_reaches_its_members(room):
    host, guest, link, agent = room
    _stub(host)
    _stub(guest)
    host.give_to_agents("test", "func_80010000")
    assert guest.claim(agent, "test").split() == ["CLAIMED", "func_80010000"]  # by strategy, despite the list
    assert _status(guest) == "open" and json.loads(guest.store.one(
        "SELECT meta FROM functions WHERE name='func_80010000'")["meta"])["for_agents"]
    guest.release(agent, "func_80010000", "", "test")
    other = guest.local_agent("claude-2")[0]
    assert guest.claim(other, "test", "func_80010000").split() == ["CLAIMED", "func_80010000"]  # and by name


def test_a_host_that_never_opened_its_overview_still_sets_stubs_aside(room):
    host, guest, link, agent = room
    _stub(host, status="open")  # the code of a BIOS stub, never looked at by the host's detectors
    assert guest.claim(agent, "test").split() == ["CLAIMED", "shop_open"]
    assert _status(host) == "asm"


def test_a_claim_on_a_function_set_aside_since_is_let_go(room):
    host, guest, link, agent = room
    hagent = host.local_agent("claude-9")[0]
    with host.store.tx() as db:
        db.execute("UPDATE functions SET n_insns=1 WHERE name='func_80010000'")
    assert host.claim_row(hagent, "test")["name"] == "func_80010000"
    _stub(host)  # set aside while held
    assert host.claim_row(hagent, "test")["name"] == "shop_open"
