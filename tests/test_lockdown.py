"""Fleet lockdown: after a runaway agent swarm (an external opencode service spawning worker bridges
under pokemon names), the app can refuse to CREATE new agent identities. Existing identities keep
working — only new names are refused — so a spawner that keeps trying new workers gets nothing.

This is the durable half of the fix: killing processes stops one swarm, the lockdown stops the next
one from minting anything at all.
"""

from __future__ import annotations

import json

import pytest

from decompnet.app.engine import RomNetwork, _lockdown_refuses, _lockdown_settings
from decompnet.network import NetworkError

from .test_permute import make_project


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".decomp").mkdir(parents=True)
    monkeypatch.setenv("DECOMP_HOME", str(h))
    monkeypatch.delenv("DECOMP_LOCKDOWN", raising=False)
    return h


@pytest.fixture
def net(tmp_path, home):
    # the lockdown belongs to the instance that owns <home>/.decomp/decomp.db
    (home / ".decomp").mkdir(parents=True, exist_ok=True)
    n = RomNetwork(home / ".decomp" / "decomp.db", tmp_path / "work")
    make_project(n, 2)
    return n


def set_settings(home, **kw):
    (home / ".decomp" / "settings.json").write_text(json.dumps(kw), encoding="utf-8")


def test_no_settings_means_no_lockdown(net):
    assert _lockdown_settings(net.db_path).get("lockdown") in (None, False)
    agent, _ = net.local_agent("brand-new-runner")
    assert agent.name == "brand-new-runner"


def test_lockdown_refuses_new_identities_but_keeps_existing_ones(net, home):
    first, token = net.chat_agent("worker", client="bridge-a")
    set_settings(home, lockdown=True)
    # an existing identity still gets a fresh token (the user's own agents keep working)
    again, token2 = net.chat_agent("worker", client="bridge-a")
    assert again.id == first.id and token2 != token
    # a second session wanting a suffixed identity is a NEW name: refused
    with pytest.raises(NetworkError, match="fleet lockdown"):
        net.chat_agent("worker", client="bridge-b")
    # a brand-new name is refused, by mint and by the runner path
    with pytest.raises(NetworkError, match="fleet lockdown"):
        net.chat_agent("tyranitar")
    with pytest.raises(NetworkError, match="fleet lockdown"):
        net.local_agent("gyarados")
    assert net.store.one("SELECT id FROM agents WHERE name='tyranitar'") is None
    assert net.store.one("SELECT id FROM agents WHERE name='gyarados'") is None
    assert net.store.one("SELECT id FROM agents WHERE name='worker_2'") is None


def test_allowed_agents_bypass_the_lockdown(net, home):
    set_settings(home, lockdown=True, allowed_agents=["lead-match"])
    assert net.local_agent("lead-match")[0].name == "lead-match"
    with pytest.raises(NetworkError, match="fleet lockdown"):
        net.local_agent("pikachu")


def test_env_override_wins_both_ways(net, home, monkeypatch):
    set_settings(home, lockdown=False)
    monkeypatch.setenv("DECOMP_LOCKDOWN", "1")
    with pytest.raises(NetworkError, match="fleet lockdown"):
        net.local_agent("snorlax")
    set_settings(home, lockdown=True)
    monkeypatch.setenv("DECOMP_LOCKDOWN", "0")
    assert net.local_agent("snorlax")[0].name == "snorlax"


def test_malformed_settings_never_lock_the_app_out(net, home, monkeypatch):
    (home / ".decomp" / "settings.json").write_text("{not json", encoding="utf-8")
    monkeypatch.delenv("DECOMP_LOCKDOWN", raising=False)
    assert _lockdown_refuses("anything", net.db_path) is None
    assert net.local_agent("still-works")[0].name == "still-works"


def test_refusal_names_the_escape_hatch(home):
    set_settings(home, lockdown=True)
    why = _lockdown_refuses("gengar", home / ".decomp" / "decomp.db")
    assert why and "settings.json" in why and "allowed_agents" in why


def test_lockdown_never_leaks_into_another_instance(tmp_path, home):
    """The regression this scoping exists for: an operator lockdown (or a stray test) in one app home
    must not stop a different database — a scratch copy, a test, a second instance — from creating its
    own agent identities. Before the fix every RomNetwork in the process inherited ~/Decomp's policy."""
    set_settings(home, lockdown=True)
    other = RomNetwork(tmp_path / "scratch.db", tmp_path / "work2")
    make_project(other, 2)
    assert _lockdown_settings(other.db_path) == {}
    assert _lockdown_refuses("tyranitar", other.db_path) is None
    assert other.local_agent("tyranitar")[0].name == "tyranitar"
    # and the guarded instance is still guarded
    assert _lockdown_refuses("tyranitar", home / ".decomp" / "decomp.db") is not None


def test_a_helper_the_lockdown_refuses_ends_in_error_and_is_not_resumed(net, home, tmp_path):
    """The refusal used to kill the helper's thread silently: the session sat at "starting" forever and
    was resumed (and refused again) after every restart."""
    from decompnet.app.agents import Manager, Session

    set_settings(home, lockdown=True)
    manager = Manager(net, tmp_path / "agent-home", "http://127.0.0.1:1/mcp")
    persisted = []
    manager.persist = persisted.append
    s = Session("claude-1", "claude", "Claude Code #1", "fixture", "claude-1", {})
    manager.sessions[s.id] = s
    manager._run(s)  # (the thread's body, run here)
    assert s.state == "error" and "fleet lockdown" in s.error
    assert any(f["type"] == "error" and "fleet lockdown" in f["text"] for f in s.feed)
    assert manager.running() == [] and persisted and persisted[-1] == []


def test_an_autopilot_the_lockdown_refuses_ends_with_the_reason(net, home):
    import threading

    set_settings(home, lockdown=True)
    state = {"state": "running", "total": 0, "tried": 0, "matched": 0, "near": 0, "twins": 0, "phase": "twins",
             "no_drafts": "", "started_at": 0, "stop": threading.Event()}
    net.autopilots["fixture"] = state
    net._autopilot_run("fixture", [], state, 1)
    assert state["state"] == "error" and "fleet lockdown" in state["error"] and state.get("finished_at")
    assert "fleet lockdown" in net.autopilot_status("fixture")["error"]


def test_a_permuter_the_lockdown_refuses_ends_with_the_reason(net, home):
    """local_agent ran outside the permuter loop's try: the refusal killed its thread silently."""
    from decompnet.app.permute import State

    set_settings(home, lockdown=True)
    st = net.permuter.jobs["fixture"] = State()
    net.permuter._loop("fixture", st)  # (the thread's body, run here)
    assert st.state == "error" and "fleet lockdown" in st.error
    assert "fleet lockdown" in net.permuter.status("fixture")["error"]


def test_a_twins_pass_the_lockdown_refuses_ends_with_the_reason(net, home):
    set_settings(home, lockdown=True)
    job = net.twins.full_pass("fixture")
    assert job.state == "error" and "fleet lockdown" in job.error
    assert job.ordered.is_set()  # agents waiting for the claim order are not left hanging
