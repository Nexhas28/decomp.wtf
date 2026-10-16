"""Disconnecting an outside agent from the Agents tab: what it claimed goes back to the others, and its token is
refused with a message that says why (not "invalid API token", which `decomp mcp` answers by getting a new identity)."""

import pytest
from starlette.testclient import TestClient

from decompnet.app import bridge
from decompnet.app.engine import RomNetwork
from decompnet.app.server import build
from decompnet.network import NetworkError

PORT, SECRET = 6498, "s3cret-for-disconnect"


def _project(net, tmp_path):
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, root, config) VALUES(1, 'game', 'game', ?, '{}')", (str(tmp_path),))
        for fid in (1, 2, 3):
            db.execute("INSERT INTO functions(id, project_id, name) VALUES(?, 1, ?)", (fid, f"f{fid}"))


def _claim(net, agent, fid):
    with net.store.tx() as db:
        db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fid, agent.id, 0, 1e12))


@pytest.fixture
def net(tmp_path):
    net = RomNetwork(tmp_path / "decomp.db", tmp_path / "work")
    _project(net, tmp_path)
    return net


def test_disconnecting_frees_its_claims_and_refuses_its_token(net):
    kicked, token = net.chat_agent("claude-code", client="bridge-a")
    other, _ = net.chat_agent("codex", client="bridge-b")
    _claim(net, kicked, 1)
    _claim(net, kicked, 2)
    _claim(net, other, 3)

    assert sorted(net.disconnect_agent("claude-code")) == ["f1", "f2"]
    held = net.store.all("SELECT function_id, agent_id FROM claims")
    assert [(r["function_id"], r["agent_id"]) for r in held] == [(3, other.id)]  # only its own claims went
    assert net.store.one("SELECT abandons FROM functions WHERE id=1")["abandons"] == 0  # not the function's fault
    with pytest.raises(NetworkError) as e:
        net.authenticate(token)
    assert "disconnected by the user" in str(e.value)
    assert not bridge._rejected(str(e.value))  # the bridge passes it on instead of quietly reconnecting
    with pytest.raises(NetworkError, match="invalid API token"):
        net.authenticate("dn_never-issued")
    net.disconnect_agent("claude-code")  # twice: still refused, nothing breaks
    with pytest.raises(NetworkError, match="disconnected by the user"):
        net.authenticate(token)


def test_a_disconnected_function_can_be_claimed_by_someone_else(net):
    kicked, _ = net.local_agent("claude-14")
    _claim(net, kicked, 1)
    net.disconnect_agent("claude-14")
    assert net.store.one("SELECT 1 FROM claims WHERE function_id=1") is None
    newcomer, _ = net.chat_agent("opencode", client="bridge-c")
    _claim(net, newcomer, 1)  # the slot is free: nothing collides


def test_a_new_identity_still_works_after_a_disconnect(net):
    net.chat_agent("claude-code", client="bridge-a")
    net.disconnect_agent("claude-code")
    fresh, token = net.chat_agent("claude-code", client="bridge-z")  # a new session, started by the user
    assert net.authenticate(token).id == fresh.id


def test_only_outside_agents_can_be_disconnected(net):
    with pytest.raises(NetworkError, match="no agent named"):
        net.disconnect_agent("nobody")
    net.local_agent("you")
    with pytest.raises(NetworkError, match="isn't an agent connected"):
        net.disconnect_agent("you")
    net._remote_agent("alice")
    with pytest.raises(NetworkError, match="isn't an agent connected"):
        net.disconnect_agent("@alice")


def test_the_endpoint_disconnects_and_keeps_helpers_for_their_stop_button(tmp_path):
    home = tmp_path / "home"
    (home / ".decomp").mkdir(parents=True)
    net = RomNetwork(home / ".decomp" / "decomp.db", home)
    _project(net, tmp_path)
    mcp, manager, security = build(net, home / ".decomp", PORT, SECRET)
    client = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"),
                        base_url=f"http://127.0.0.1:{PORT}")
    client.cookies.set("decomp_live", SECRET)
    h = {"X-Decomp": "1"}

    kicked, token = net.chat_agent("claude-code", client="bridge-a")
    _claim(net, kicked, 1)
    manager.assignments[kicked.id] = "game"
    assert client.post("/api/agents/disconnect", json={"name": "claude-code"}).status_code == 403  # needs the header
    r = client.post("/api/agents/disconnect", json={"name": "claude-code"}, headers=h)
    assert r.status_code == 200 and r.json()["released"] == ["f1"]
    assert kicked.id not in manager.assignments
    assert net.store.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0
    with pytest.raises(NetworkError, match="disconnected by the user"):
        net.authenticate(token)

    from types import SimpleNamespace
    manager.sessions["s1"] = SimpleNamespace(agent_name="claude-3", state="working")
    r = client.post("/api/agents/disconnect", json={"name": "claude-3"}, headers=h)
    assert r.status_code == 400 and "Stop" in r.json()["error"]
    del manager.sessions["s1"]
    assert client.post("/api/agents/disconnect", json={"name": "@alice"}, headers=h).status_code == 400
