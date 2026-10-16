"""Team Call: Opus, Sonnet and Haiku on one function together, until it matches, its limits, or switched off."""

import time

import pytest
from starlette.testclient import TestClient

from decompnet.app import agents
from decompnet.app.engine import RomNetwork
from decompnet.app.server import build
from decompnet.network import NetworkError


@pytest.fixture
def team_app(tmp_path, monkeypatch):
    monkeypatch.setattr(agents.Manager, "_run", lambda self, s: None)  # no real helpers in tests
    home = tmp_path / "home"
    (home / ".decomp").mkdir(parents=True)
    net = RomNetwork(home / ".decomp" / "decomp.db", home)
    mcp, manager, security = build(net, home / ".decomp", 6493, "k")
    c = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"), base_url="http://127.0.0.1:6493")
    c.cookies.set("decomp_live", "k")
    h = {"X-Decomp": "1"}
    job = c.post("/api/import?demo=1", content=b"", headers=h).json()["job"]
    for _ in range(600):
        j = c.get(f"/api/job?id={job}").json()
        if j["state"] != "running":
            break
        time.sleep(0.1)
    if j["state"] != "done":
        pytest.skip(f"the demo didn't import here: {j.get('error')}")
    slug = j["result"]["slug"]
    for _ in range(600):  # the autopilot's free matches first: then what's open stays open
        if (c.get(f"/api/live?slug={slug}").json()["overview"].get("autopilot") or {}).get("state") != "running":
            break
        time.sleep(0.1)
    open_fn = net.store.one("SELECT f.name FROM functions f JOIN projects p ON p.id=f.project_id WHERE p.slug=? "
                            "AND f.status='open' ORDER BY f.n_insns LIMIT 1", slug)["name"]
    manager.anthropic_key = lambda: "sk-ant-api03-test"
    return c, net, manager, slug, open_fn, h


def test_three_models_share_one_function_and_others_stay_out(team_app):
    c, net, manager, slug, fn, h = team_app
    st = c.post("/api/team", json={"slug": slug, "function": fn}, headers=h).json()
    assert st.get("active"), st
    assert [m["model"] for m in st["members"]] == ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
    s = manager.sessions[st["members"][2]["id"]]
    assert s.options["billing"] == "api" and s.options["team"] == {"function": fn, "role": "opus"}
    assert "never call claim_function" in manager._prompt(s) and fn in manager._prompt(s)
    assert all(r["agent"].startswith(("team-", "nuke")) for r in manager.running())  # remembered to clean up after a crash, never resumed
    members = [net.local_agent(m["id"])[0] for m in st["members"]]
    for m in members:  # every member may compile it (a compile error still passed the claim check)
        assert "COMPILE ERROR" in net.attempt(m, fn, "this is not C", slug)["text"]
    outsider, _ = net.local_agent("claude-9")
    with pytest.raises(NetworkError, match="is claimed by"):
        net.attempt(outsider, fn, "int x;", slug)
    # switching it off stops all three and lets the function go
    off = c.post("/api/team", json={"slug": slug, "function": fn, "on": False}, headers=h).json()
    assert not off["active"] and off["reason"] == "switched off"
    assert all(manager.sessions[m["id"]].stop.is_set() for m in st["members"])
    assert net.store.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0


def test_the_call_ends_at_the_first_match(team_app):
    c, net, manager, slug, fn, h = team_app
    st = c.post("/api/team", json={"slug": slug, "function": fn}, headers=h).json()
    with net.store.tx() as db:  # one of them matched it
        db.execute("UPDATE functions SET status='matched', best_score=0 WHERE name=?", (fn,))
    for _ in range(50):
        t = c.get(f"/api/team?slug={slug}&function={fn}").json()
        if not t["active"]:
            break
        time.sleep(0.2)
    assert t["reason"] == "matched" and t["matched"]
    assert all(manager.sessions[m["id"]].stop.is_set() for m in st["members"])


def test_needs_the_key_and_a_free_function(team_app):
    c, net, manager, slug, fn, h = team_app
    manager.anthropic_key = lambda: ""
    assert "API key" in c.post("/api/team", json={"slug": slug, "function": fn}, headers=h).json()["error"]
    manager.anthropic_key = lambda: "sk-ant-api03-test"
    other, _ = net.local_agent("codex-3")
    net.attempt(other, fn, "this is not C", slug)  # another agent took it
    assert "being worked on by codex-3" in c.post("/api/team", json={"slug": slug, "function": fn}, headers=h).json()["error"]


def test_claude_code_spend_is_counted_while_it_runs():
    import json
    state: dict = {}
    msg = {"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5", "content": [],
                                             "usage": {"input_tokens": 1000, "output_tokens": 0}}}
    first = agents.parse_line("claude", json.dumps(msg), state)
    msg["message"]["usage"]["output_tokens"] = 1000  # the same reply, now finished
    second = agents.parse_line("claude", json.dumps(msg), state)
    spent = [json.loads(t)["usd"] for k, t in first + second if k == "usage"]
    assert spent == [pytest.approx(0.004), pytest.approx(0.02)]
    end = agents.parse_line("claude", json.dumps({"type": "result", "total_cost_usd": 0.03}), state)
    assert [json.loads(t)["usd"] for k, t in end if k == "usage"] == [pytest.approx(0.006)]  # total 0.03 exactly


def test_function_nuke_works_on_several_functions_at_once(team_app):
    c, net, manager, slug, fn, h = team_app
    two = [r["name"] for r in net.store.all("SELECT f.name FROM functions f JOIN projects p ON p.id=f.project_id "
                                            "WHERE p.slug=? AND f.status='open' ORDER BY f.n_insns LIMIT 2", slug)]
    for name in two:  # the skill's name may hold both; each attempt is judged as usual
        r = c.post("/api/try", json={"slug": slug, "name": name, "source": "not C", "agent": "function-nuke"}, headers=h)
        assert r.status_code == 200 and r.json()["score"] is None
    holders = {row["name"] for row in net.store.all("SELECT a.name FROM claims c JOIN agents a ON a.id=c.agent_id")}
    assert holders == {"function-nuke"}
    outsider, _ = net.local_agent("claude-7")
    with pytest.raises(NetworkError, match="claimed by function-nuke"):
        net.attempt(outsider, two[0], "int x;", slug)
    # any other agent (you included) still holds one function at a time
    you, _ = net.local_agent("you")
    others = [r["name"] for r in net.store.all("SELECT f.name FROM functions f JOIN projects p ON p.id=f.project_id "
                                               "WHERE p.slug=? AND f.status='open' ORDER BY f.n_insns LIMIT 2 OFFSET 2", slug)]
    net.attempt(you, others[0], "not C", slug)
    with pytest.raises(NetworkError, match="you hold another claim"):
        net.attempt(you, others[1], "not C", slug)
    assert c.post("/api/try", json={"slug": slug, "name": two[0], "source": "x", "agent": "nobody"}, headers=h).status_code == 400
    assert c.post("/api/release", json={"slug": slug, "name": two[0], "agent": "function-nuke"}, headers=h).json() == {"ok": True}


def test_the_nuke_button_runs_ten_haiku_in_waves_on_several_functions(team_app):
    c, net, manager, slug, fn, h = team_app
    manager.anthropic_key = lambda: ""  # a Nuke runs on the Claude login: no API key needed
    other = net.store.one("SELECT f.name FROM functions f JOIN projects p ON p.id=f.project_id WHERE p.slug=? "
                          "AND f.status='open' AND f.name<>? ORDER BY f.n_insns LIMIT 1", slug, fn)["name"]
    sts = [c.post("/api/team", json={"slug": slug, "function": f, "kind": "nuke"}, headers=h).json() for f in (fn, other)]
    for st, f in zip(sts, (fn, other)):
        assert st.get("active") and st["kind"] == "nuke" and st["billing"] == "plan", st
        assert len(st["members"]) == 10 and {m["model"] for m in st["members"]} == {"claude-haiku-5-5"}
        s = manager.sessions[st["members"][3]["id"]]
        assert "billing" not in s.options and s.options["team"]["waves"] == agents.NUKE_WAVES
        prompt = manager._prompt(s)
        assert "Haiku #4" in prompt and agents.NUKE_ANGLES[3] in prompt and f in prompt
        assert "never call claim_function" in prompt
        # every member may compile its own function, both functions at once
        m = net.local_agent(st["members"][0]["id"])[0]
        assert "COMPILE ERROR" in net.attempt(m, f, "this is not C", slug)["text"]
    assert all(r["agent"].startswith(("team-", "nuke")) for r in manager.running())  # remembered to clean up after a crash, never resumed
    # when every helper has done its waves, the nuke ends and lets the function go
    for m in sts[0]["members"]:
        s = manager.sessions[m["id"]]
        s.runs, s.state = agents.NUKE_WAVES, "stopped"
    for _ in range(50):
        t = c.get(f"/api/team?slug={slug}&function={fn}").json()
        if not t["active"]:
            break
        time.sleep(0.2)
    assert t["reason"] == f"no match after {agents.NUKE_WAVES} waves"
    assert c.get(f"/api/team?slug={slug}&function={other}").json()["active"]
    off = c.post("/api/team", json={"slug": slug, "function": other, "on": False}, headers=h).json()
    assert not off["active"]
    assert net.store.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0
