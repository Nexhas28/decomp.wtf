"""Claude Code helpers billed to the Anthropic API key (API credits) instead of the user's Claude login."""

import pytest
from starlette.testclient import TestClient

from decompnet.app import agents
from decompnet.app.engine import RomNetwork
from decompnet.app.server import build


def _manager(tmp_path, monkeypatch, kind):
    (tmp_path / "bin").mkdir(exist_ok=True)
    exe = tmp_path / "bin" / kind
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(agents, "_which", lambda cmd: str(exe))
    m = agents.Manager.__new__(agents.Manager)
    m.mcp_url = "http://127.0.0.1:6464/mcp"
    m.prompt = lambda s: "work"
    return m


def test_api_billing_uses_the_key_and_never_the_login(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "login-token")
    m = _manager(tmp_path, monkeypatch, "claude")
    m.anthropic_key = lambda: "sk-ant-api03-test-1234"
    s = agents.Session("claude-1", "claude", "claude", "demo", "claude-1",
                       {"model": "claude-opus-5-5", "effort": "medium", "billing": "api"})
    (tmp_path / "w").mkdir()
    cmd, env, _ = m._command(s, "tok", tmp_path / "w")
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5" and cmd[cmd.index("--effort") + 1] == "medium"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-api03-test-1234" and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    # without the key it doesn't start, rather than run on the login
    m.anthropic_key = lambda: ""
    with pytest.raises(RuntimeError, match="no Anthropic API key"):
        m._command(s, "tok", tmp_path / "w")
    # a normal helper keeps the login and gets no key
    plain = agents.Session("claude-2", "claude", "claude", "demo", "claude-2", {})
    _, env2, _ = m._command(plain, "tok", tmp_path / "w")
    assert "ANTHROPIC_API_KEY" not in env2 or env2["ANTHROPIC_API_KEY"] != "sk-ant-api03-test-1234"


def test_the_key_is_kept_masked_and_required(tmp_path):
    home = tmp_path / "home"
    (home / ".decomp").mkdir(parents=True)
    mcp, _, security = build(RomNetwork(home / ".decomp" / "decomp.db", home), home / ".decomp", 6496, "k")
    c = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"), base_url="http://127.0.0.1:6496")
    c.cookies.set("decomp_live", "k")
    h = {"X-Decomp": "1"}
    assert c.post("/api/settings", json={"anthropic_api_key": "nope"}, headers=h).status_code == 400
    assert c.post("/api/settings", json={"anthropic_api_key": "sk-ant-api03-secret-9876"}, headers=h).status_code == 200
    state = c.get("/api/state").text
    assert "secret" not in state and "••••••••9876" in state
    r = c.post("/api/agents/start", json={"kind": "codex", "slug": "x", "options": {"billing": "api"}}, headers=h)
    assert r.status_code == 400
    c.post("/api/settings", json={"anthropic_api_key": ""}, headers=h)
    assert '"anthropic_key": ""' in c.get("/api/state").text.replace('":"', '": "')


def test_claude_code_on_credits_is_checked_before_it_works():
    assert agents.claude_key_source('{"type":"system","subtype":"init","apiKeySource":"ANTHROPIC_API_KEY"}') == "ANTHROPIC_API_KEY"
    assert agents.claude_key_source('{"type":"system","subtype":"init","apiKeySource":"none"}') == "none"
    assert agents.claude_key_source('{"type":"assistant"}') is None


def test_subscription_keys_are_refused(tmp_path):
    home = tmp_path / "home"
    (home / ".decomp").mkdir(parents=True)
    mcp, _, security = build(RomNetwork(home / ".decomp" / "decomp.db", home), home / ".decomp", 6495, "k")
    c = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"), base_url="http://127.0.0.1:6495")
    c.cookies.set("decomp_live", "k")
    for key in ("sk-ant-usr-abc", "sk-ant-oat01-abc"):
        r = c.post("/api/settings", json={"anthropic_api_key": key}, headers={"X-Decomp": "1"})
        assert r.status_code == 400 and "Claude plan" in r.json()["error"]


def test_settings_can_clear_every_function_note(tmp_path):
    home = tmp_path / "home"
    (home / ".decomp").mkdir(parents=True)
    net = RomNetwork(home / ".decomp" / "decomp.db", home)
    with net.store.tx() as db:
        db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(1,5,1,'on a function',0)")
        db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(1,6,1,'another',0)")
        db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(1,NULL,1,'project-wide',0)")
    mcp, _, security = build(net, home / ".decomp", 6494, "k")
    c = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"), base_url="http://127.0.0.1:6494")
    c.cookies.set("decomp_live", "k")
    assert c.post("/api/notes/clear", json={}).status_code == 403  # a write needs the header
    assert c.post("/api/notes/clear", json={}, headers={"X-Decomp": "1"}).json() == {"cleared": 2}
    assert [r["text"] for r in net.store.all("SELECT text FROM notes")] == ["project-wide"]
