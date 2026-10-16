"""Researcher helpers: they claim nothing, pick open functions with next_to_research, leave research notes that stay
pinned first in the next agent's brief, and add sourced know-how to the project's knowledge base (.okf). Stub-based:
no compiler, no agent CLI, and the knowledge base is a copy in a temporary folder (never the checkout's .okf)."""

from __future__ import annotations

import datetime
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from decompnet.app import agents, chat, okf, tools
from decompnet.app.engine import RomNetwork
from decompnet.app.server import build
from decompnet.network import RESEARCH_PREFIX, NetworkError

ROOT = Path(__file__).resolve().parent.parent
GITHUB_RULES = ["WebFetch(domain:github.com)", "WebFetch(domain:api.github.com)",
                "WebFetch(domain:raw.githubusercontent.com)", "WebFetch(domain:gist.github.com)"]


@pytest.fixture
def net(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".decomp").mkdir(parents=True)
    n = RomNetwork(home / ".decomp" / "decomp.db", home)
    with n.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'fixture', 'fixture', 'research fixture', 'mips', 0, ?, '{\"kind\": \"rom\"}')",
                   (str(tmp_path),))
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, '', NULL, 'fixture', 0, '')")
        for i, (name, n_insns) in enumerate((("a_small", 10), ("b_mid", 20), ("c_big", 30), ("d_huge", 40)), start=1):
            db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, seed, status, "
                       "attempts, abandons, best_score, best_source, vram) "
                       "VALUES(?,1,?,?,?,?,'[]','nop','/* m2c */','open',0,0,NULL,NULL,?)",
                       (i, name, n_insns * 4, n_insns, b"\0" * (n_insns * 4), 0x80001000 + i * 0x100))
    monkeypatch.setattr(n, "_extra_brief", lambda fn, proj: [])  # the ROM brief's map block needs a real import
    return n


def agent(net, name, role=""):
    a = net.authenticate(net.register_agent(name))
    if role:
        net.role[a.id] = role
    return a


def fid(net, name):
    return net.store.one("SELECT id FROM functions WHERE name=?", name)["id"]


# ------------------------------------------------------------------------------------------- starting one


def test_starting_a_researcher_checks_the_job_and_swaps_tiered_for_a_fixed_model(net, monkeypatch):
    mcp, manager, security = build(net, net.workspace / ".decomp", 6496, "k")
    monkeypatch.setattr(agents.Manager, "_run", lambda self, s: None)  # no CLI here: the start path is the test
    c = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"), base_url="http://127.0.0.1:6496")
    c.cookies.set("decomp_live", "k")
    h = {"X-Decomp": "1"}
    r = c.post("/api/agents/start", json={"kind": "claude", "slug": "fixture", "options": {"role": "wizard"}}, headers=h)
    assert r.status_code == 400 and "unknown job" in r.json()["error"]
    # the launcher's default for Claude Code is tiered; a leftover Focus doesn't apply to research and isn't checked
    r = c.post("/api/agents/start", json={"kind": "claude", "slug": "fixture", "count": 1, "options": {
        "role": "researcher", "model": "tiered", "per_session": 3, "focus": "close"}}, headers=h)
    assert r.status_code == 200, r.text
    s = manager.sessions[r.json()["started"][0]]
    assert s.options["role"] == "researcher" and not s.tiered and "focus" not in s.options
    assert (s.options["model"], s.options["effort"]) == (agents.RESEARCH_MODEL, "medium") == ("claude-sonnet-5-5", "medium")
    assert s.options["per_session"] == 3  # not tiered's one function per session
    assert s.snapshot()["role"] == "researcher"
    # no model (the chat agent's start_agents) gets the role's model too; a model picked by name is kept
    r = c.post("/api/agents/start", json={"kind": "claude", "slug": "fixture", "options": {"role": "researcher"}},
               headers=h)
    s = manager.sessions[r.json()["started"][0]]
    assert (s.options["model"], s.options["effort"]) == (agents.RESEARCH_MODEL, agents.RESEARCH_EFFORT)
    r = c.post("/api/agents/start", json={"kind": "claude", "slug": "fixture", "options": {
        "role": "researcher", "model": "claude-opus-5-5"}}, headers=h)
    s = manager.sessions[r.json()["started"][0]]
    assert s.options["model"] == "claude-opus-5-5" and "effort" not in s.options
    # a matching helper is unchanged: tiered, no role
    r = c.post("/api/agents/start", json={"kind": "claude", "slug": "fixture", "options": {"model": "tiered"}}, headers=h)
    m = manager.sessions[r.json()["started"][0]]
    assert m.tiered and m.snapshot()["role"] == "" and "role" not in m.options
    with pytest.raises(ValueError, match="unknown job"):
        manager._start("claude", "fixture", 1, {"role": "nope"})
    # the chat agent's start_agents takes the job too
    assert chat.agent_options({}, role="researcher")["role"] == "researcher"
    with pytest.raises(ValueError):
        chat.agent_options({}, role="nope")


def _cli_manager(tmp_path, monkeypatch):
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(agents, "_which", lambda cmd: str(exe))
    m = agents.Manager.__new__(agents.Manager)
    m.mcp_url = "http://127.0.0.1:6464/mcp"
    m.prompt = lambda s: "work"
    m.routing_config = lambda: {}
    return m


def test_only_a_claude_code_researcher_may_read_github(tmp_path, monkeypatch):
    m = _cli_manager(tmp_path, monkeypatch)
    (tmp_path / "w").mkdir()
    cfg = str(tmp_path / "w" / "mcp.json")
    matcher = agents.Session("claude-1", "claude", "c", "fixture", "claude-1", {"model": "opus"})
    cmd, _, _ = m._command(matcher, "tok", tmp_path / "w")
    assert cmd == [cmd[0], "-p", "--output-format", "stream-json", "--verbose", "--mcp-config", cfg, "--strict-mcp-config",
                   "--allowedTools", "mcp__decomp", "--disallowedTools", "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch",
                   "--model", "opus"]  # exactly as before researchers existed
    research = agents.Session("claude-2", "claude", "c", "fixture", "claude-2", {"model": "opus", "role": "researcher"})
    cmd, _, _ = m._command(research, "tok", tmp_path / "w")
    assert cmd == [cmd[0], "-p", "--output-format", "stream-json", "--verbose", "--mcp-config", cfg, "--strict-mcp-config",
                   "--allowedTools", ",".join(["mcp__decomp", *GITHUB_RULES]),
                   "--disallowedTools", "Bash,Edit,Write,NotebookEdit,WebSearch", "--model", "opus"]


# ------------------------------------------------------------------------------------------- picking work


def test_next_to_research_skips_claimed_researched_and_reserved_functions(net):
    r1, r2 = agent(net, "researcher-1", "researcher"), agent(net, "researcher-2", "researcher")
    matcher = agent(net, "matcher")
    # same order as the default claim: fewest instructions first
    assert [f["name"] for f in net.research_candidates("fixture", r1.id, 3)] == ["a_small", "b_mid", "c_big"]
    assert net._research_held == {}  # a peek reserves nothing
    net.claim(matcher, "fixture", "a_small", "easiest")  # a matcher holds it: not researched
    net.add_research_note(r2, "fixture", "b_mid", "already done")  # researched already
    out = net.next_to_research(r1, "fixture", 1)
    assert "c_big" in out and "a_small" not in out and "b_mid" not in out and "0x80001300" in out
    assert "claim_function" in out  # ...told never to claim it
    # c_big is r1's for now: r2 is handed the next one, r1 still sees its own
    assert "d_huge" in net.next_to_research(r2, "fixture", 1)
    assert [f["name"] for f in net.research_candidates("fixture", r1.id, 3)] == ["c_big"]
    assert net.research_candidates("fixture", matcher.id, 3) == []  # everything is held, claimed or researched
    # a note frees the reservation; a stopped researcher gives back what it held
    net.add_research_note(r1, "fixture", "c_big", "notes")
    assert fid(net, "c_big") not in net._research_held
    net.release_research(r2.id)
    assert net._research_held == {}
    assert [f["name"] for f in net.research_candidates("fixture", r1.id, 3)] == ["d_huge"]
    # researchers never claim: the tool refuses anyone else, and claims nothing
    with pytest.raises(NetworkError):
        net.next_to_research(matcher, "fixture", 1)
    assert net.store.one("SELECT COUNT(*) AS n FROM claims WHERE agent_id IN (?, ?)", r1.id, r2.id)["n"] == 0


def test_a_reservation_expires(net, monkeypatch):
    r1, r2 = agent(net, "researcher-1", "researcher"), agent(net, "researcher-2", "researcher")
    net.research_candidates("fixture", r1.id, 1, reserve=True)
    assert net.research_candidates("fixture", r2.id, 1)[0]["name"] == "b_mid"
    import decompnet.app.engine as engine
    real = engine.time.time
    monkeypatch.setattr(engine.time, "time", lambda: real() + engine.RESEARCH_HOLD + 1)
    assert net.research_candidates("fixture", r2.id, 1)[0]["name"] == "a_small"


# ------------------------------------------------------------------------------------------- research notes


def test_the_research_note_stays_first_after_ten_newer_notes(net, monkeypatch):
    r, m = agent(net, "researcher-1", "researcher"), agent(net, "matcher")
    net.add_research_note(r, "fixture", "c_big", f"{RESEARCH_PREFIX}Likely a list walk; copy b_mid's loop.")
    for i in range(10):
        net.add_note(m, "fixture", f"tried variant {i}", "c_big")
    brief = net.get_function("c_big", "fixture")
    pinned = brief.index("### Research notes (read first)")
    assert pinned < brief.index("### Notes from previous agents")
    assert "Likely a list walk; copy b_mid's loop." in brief and RESEARCH_PREFIX not in brief  # the prefix once, hidden
    assert "tried variant 9" in brief and "tried variant 2" in brief and "tried variant 1" not in brief  # 8 newest
    stored = net.store.one("SELECT text FROM notes WHERE agent_id=?", r.id)["text"]
    assert stored == RESEARCH_PREFIX + "Likely a list walk; copy b_mid's loop."
    # the function view in the app: pinned on top, marked for the page to highlight
    monkeypatch.setattr(net, "xrefs", lambda proj: {"calls": {}, "called_by": {}})
    monkeypatch.setattr(net, "symbols", lambda pid: SimpleNamespace(label=str))
    detail = net.function_detail("fixture", "c_big")
    assert detail["notes"][0] == {"text": "Likely a list walk; copy b_mid's loop.", "agent": "researcher-1",
                                  "created_at": detail["notes"][0]["created_at"], "research": True}
    assert len(detail["notes"]) == 11 and not any(n.get("research") for n in detail["notes"][1:])


def test_only_researchers_write_research_notes_and_knowledge(net, tmp_path, monkeypatch):
    monkeypatch.setenv("DECOMP_OKF", str(tmp_path / "okf"))
    matcher, r = agent(net, "matcher"), agent(net, "researcher-1", "researcher")
    for name, args in (("add_research_note", {"function": "a_small", "text": "x"}), ("next_to_research", {}),
                       ("add_knowledge", {"title": "t", "description": "d", "text": "x",
                                          "sources": ["https://github.com/a/b"]})):
        assert tools.call(net, matcher, "fixture", name, args).startswith("error:"), name
    assert "Saved" in tools.call(net, r, "fixture", "add_research_note", {"function": "a_small", "text": "tips"})
    chat_agent = net.authenticate(net.register_agent("chat-1", origin="chat"))
    assert "Saved as research/" in tools.call(net, chat_agent, "fixture", "add_knowledge", {
        "title": "Chat find", "description": "d", "text": "x", "sources": ["https://github.com/a/b"]})


def test_tool_scoping_by_role():
    for fork in (False, True):
        matcher = set(tools.tool_names(fork))
        assert {"search_knowledge", "read_knowledge"} <= matcher and not tools.RESEARCH_ONLY & matcher
        assert tools.RESEARCH_ONLY | {"search_knowledge", "read_knowledge", "get_function", "find_similar"} <= set(
            tools.tool_names(fork, "researcher"))
    names = lambda role=None: {d["function"]["name"] for d in agents.api_tool_schemas(role=role)}
    assert {"search_knowledge", "read_knowledge"} <= names() and not tools.RESEARCH_ONLY & names()
    assert tools.RESEARCH_ONLY <= names("researcher")


# ------------------------------------------------------------------------------------------- the run loop


def test_progress_counts_research_and_a_researcher_stops_when_nothing_is_left(net, tmp_path, monkeypatch):
    m = agents.Manager(net, tmp_path / "agent-home", "http://127.0.0.1:1/mcp")
    r = agent(net, "researcher-9")
    before = m._progress(r.id)
    net.add_research_note(r, "fixture", "a_small", "tips")
    assert m._progress(r.id) == before + 1  # a session that only wrote research notes made progress
    for name in ("b_mid", "c_big", "d_huge"):
        net.add_research_note(r, "fixture", name, "tips")
    monkeypatch.setattr(m, "_run_cli", lambda *a: pytest.fail("nothing to research: no session should start"))
    s = agents.Session("claude-1", "claude", "Claude Code #1", "fixture", "claude-1",
                       {"role": "researcher", "model": agents.RESEARCH_MODEL})
    m.sessions[s.id] = s
    m._run(s)
    texts = [f["text"] for f in s.feed]
    assert "role: researcher" in texts
    assert any(t.startswith("stopping: nothing left to research") for t in texts)
    assert s.state == "stopped" and net.role == {} and net.role_model == {}


def test_a_researcher_session_that_wrote_notes_is_not_a_failure(net, tmp_path, monkeypatch):
    m = agents.Manager(net, tmp_path / "agent-home", "http://127.0.0.1:1/mcp")
    s = agents.Session("claude-1", "claude", "Claude Code #1", "fixture", "claude-1",
                       {"role": "researcher", "model": agents.RESEARCH_MODEL})
    m.sessions[s.id] = s
    seen = {}

    def session(s, token, workdir):  # what a researcher does in one session: every function gets a note
        a = net.authenticate(token)
        seen["role"], seen["model"] = net.role.get(a.id), net.role_model.get(a.id)
        for row in net.research_candidates(s.slug, a.id, 4):
            net.add_research_note(a, s.slug, row["name"], "tips")
        return True
    monkeypatch.setattr(m, "_run_cli", session)
    m._run(s)
    assert seen == {"role": "researcher", "model": agents.RESEARCH_MODEL}
    assert not s.error and not any(f["type"] == "error" for f in s.feed)
    assert s.runs == 1 and any(f["text"].startswith("stopping: nothing left to research") for f in s.feed)


# ------------------------------------------------------------------------------------------- prompts


def _prompt_manager():
    stub = SimpleNamespace(
        overview=lambda slug: {"title": "Demo Quest", "kind": "rom", "platform": "n64",
                               "compiler": {"name": "IDO 7.1", "toolchain": "ido-7.1"}},
        learnings=lambda slug: "", instructions=lambda slug: "")
    m = agents.Manager.__new__(agents.Manager)
    m.net = stub
    return m


def test_a_researcher_gets_the_research_prompt_and_web_wording_only_on_claude_code():
    m = _prompt_manager()
    for kind in ("claude", "codex", "opencode", "antigravity", "api"):
        s = agents.Session(f"{kind}-1", kind, kind, "demo", f"{kind}-1", {"role": "researcher", "per_session": 3})
        text = m.prompt(s)
        assert text.startswith("You are a researcher in decomp.wtf, working on Demo Quest (n64; compiler: IDO 7.1)")
        assert "next_to_research" in text and "add_research_note" in text and "Do 3 functions" in text
        assert ("api.github.com/search/repositories" in text) == (kind == "claude"), kind
        assert ("You can't read the web" in text) == (kind != "claude"), kind
        assert "Know-how you'd otherwise read with guide()" in text  # the usual guides still come along
    matcher = agents.Session("claude-1", "claude", "c", "demo", "claude-1", {})
    assert m.prompt(matcher).startswith("You are a decompilation agent") and "researcher" not in m._prompt(matcher)


def test_the_job_menu_in_the_web_page_matches_the_roles():
    js = (ROOT / "src" / "decompnet" / "app" / "static" / "app.js").read_text(encoding="utf-8")
    menu = set(re.findall(r'\{ id: "(\w+)", name: "[^"]+"', js.split("const JOBS = ", 1)[1].split("];", 1)[0]))
    assert menu == {"match", *agents.ROLES}
    assert "options.role = LZ.role" in js


# ------------------------------------------------------------------------------------------- the knowledge base


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """A copy of the checkout's bundle's root files (its index.md with front matter, its log.md) and one concept."""
    b = tmp_path / "okf"
    (b / "tools").mkdir(parents=True)
    for name in ("index.md", "log.md"):
        shutil.copy(ROOT / ".okf" / name, b / name)
    shutil.copy(ROOT / ".okf" / "tools" / "armips.md", b / "tools" / "armips.md")
    (tmp_path / "secret.md").write_text("---\ntype: Secret\n---\nnot yours\n")
    monkeypatch.setenv("DECOMP_OKF", str(b))
    return b


def test_knowledge_is_searched_and_read_inside_the_bundle_only(bundle, tmp_path):
    hits = okf.search("armips MIPS")
    assert "tools/armips.md: armips." in hits and "MIPS and ARM assembler" in hits
    assert "Nothing in the knowledge base" in okf.search("no-such-word-anywhere")
    assert okf.read("tools/armips.md").startswith("---\ntype: Tool")
    assert "decomp.wtf knowledge base" in okf.read("index.md") and okf.read("") == okf.read("index.md")
    (bundle / "evil.md").symlink_to(tmp_path / "secret.md")
    (bundle / "evil-dir").symlink_to(tmp_path)
    for bad in ("../secret.md", "/etc/passwd", "tools/../../secret.md", "evil.md", "evil-dir/secret.md", "~/x.md",
                "C:/x.md"):
        with pytest.raises(NetworkError):
            okf.read(bad)
    assert okf.search("not yours").startswith("Nothing in the knowledge base")  # nor does the search follow links


def test_add_knowledge_writes_a_valid_sourced_concept_and_lists_it(bundle):
    yaml = pytest.importorskip("yaml")
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    out = okf.add("IDO 7.1: -O2 loop unrolling", "When IDO unrolls a loop by four, and what C gets it.",
                  "Write the loop as a plain `for`; \"quotes\" and: colons stay safe.",
                  ["https://github.com/example/cookbook/blob/main/ido.md", {"url": "https://github.com/x/y", "title": "Y"}],
                  tags=["IDO", "n64", "loops"], by="decomp.wtf/researcher (claude-sonnet-5-5)")
    assert "research/ido-7-1-o2-loop-unrolling.md" in out
    text = (bundle / "research" / "ido-7-1-o2-loop-unrolling.md").read_text()
    head, body = text.split("---\n", 2)[1:]
    meta = yaml.safe_load(head)
    assert meta["type"] == "Concept" and meta["title"] == "IDO 7.1: -O2 loop unrolling"
    assert meta["tags"] == ["ido", "n64", "loops"]
    assert meta["generated"]["by"] == "decomp.wtf/researcher (claude-sonnet-5-5)"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", str(meta["generated"]["at"]))
    assert meta["sources"] == [{"id": "s1", "resource": "https://github.com/example/cookbook/blob/main/ido.md",
                                "title": "https://github.com/example/cookbook/blob/main/ido.md"},
                               {"id": "s2", "resource": "https://github.com/x/y", "title": "Y"}]
    assert body.lstrip().startswith("# IDO 7.1: -O2 loop unrolling") and "[^s2]: Y (https://github.com/x/y)" in body
    # listed in research/index.md (no front matter), research/ linked once from the root index (front matter kept)
    index = (bundle / "research" / "index.md").read_text()
    assert index.startswith("# Research") and \
        "- [IDO 7.1: -O2 loop unrolling](./ido-7-1-o2-loop-unrolling.md) - When IDO unrolls" in index
    okf.add("GBA agbcc: mov vs lsl", "Which shift agbcc emits.", "text", ["https://github.com/pret/pokeemerald"])
    root = (bundle / "index.md").read_text()
    assert root.startswith("---\nokf_version: '0.2'\n---") and root.count("./research/") == 1
    assert yaml.safe_load(root.split("---\n")[1]) == {"okf_version": "0.2"}
    assert (bundle / "research" / "index.md").read_text().count("\n- [") == 2
    # log.md: newest first, under today's heading (one heading, entries on top)
    log = (bundle / "log.md").read_text()
    headings = re.findall(r"^## (.+)$", log, re.M)
    assert headings[0] == today and headings.count(today) == 1 and headings == sorted(headings, reverse=True)
    first = log.split(f"## {today}\n", 1)[1].splitlines()
    assert first[0].startswith("* **Creation**: Added [GBA agbcc: mov vs lsl](research/gba-agbcc-mov-vs-lsl.md)")
    assert first[1].startswith("* **Creation**: Added [IDO 7.1: -O2 loop unrolling]")


def test_add_knowledge_needs_a_source_url_and_never_overwrites(bundle):
    for bad in ([], ["not a url"], ["file:///etc/passwd"], ["tools/armips.md"]):
        with pytest.raises(NetworkError, match="source"):
            okf.add("A topic", "d", "text", bad)
    assert not (bundle / "research" / "a-topic.md").exists()
    okf.add("A topic", "d", "first", ["https://github.com/a/b"])
    with pytest.raises(NetworkError, match="exists already"):
        okf.add("A Topic!", "d", "second", ["https://github.com/a/b"])
    assert "first" in (bundle / "research" / "a-topic.md").read_text()


def test_a_new_day_gets_its_own_log_heading_on_top(bundle):
    (bundle / "log.md").write_text("# Update Log\n\n## 2020-01-01\n* **Creation**: old.\n")
    okf.add("Topic", "d", "text", ["https://github.com/a/b"])
    log = (bundle / "log.md").read_text()
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    assert log.index(f"## {today}") < log.index("## 2020-01-01") and log.startswith("# Update Log\n\n## ")


def test_researchers_never_claim_release_or_compile(net):
    r = agent(net, "researcher-1", "researcher")
    for name, args in (("claim_function", {}), ("release_function", {"function": "a_small", "notes": "x"}),
                       ("compile_and_diff", {"function": "a_small", "source": "void a_small(void) {}"})):
        out = tools.call(net, r, "fixture", name, args)
        assert out.startswith("error: researchers don't"), (name, out)
    assert net.store.one("SELECT COUNT(*) AS n FROM claims")["n"] == 0
    assert not tools.call(net, agent(net, "matcher"), "fixture", "claim_function", {}).startswith("error")


def test_in_a_room_a_researched_function_is_not_handed_out_again_before_its_note_syncs(net):
    sent = []

    class Link:  # a room member: notes go to the host and come back on a later sync
        error, last_ok = "", float("inf")

        def call(self, path, agent, **kw):
            sent.append((path, kw))
            return "Noted, and shared with the room."

        def sync(self):
            pass
    net.links["fixture"] = Link()
    r = agent(net, "researcher-1", "researcher")
    assert net.research_candidates("fixture", r.id, 1, reserve=True)[0]["name"] == "a_small"
    assert "shared with the room" in net.add_research_note(r, "fixture", "a_small", "tips")
    assert sent == [("/collab/note", {"text": RESEARCH_PREFIX + "tips", "function": "a_small"})]
    assert net.store.one("SELECT COUNT(*) AS n FROM notes")["n"] == 0  # the note is the host's, not here yet
    assert net.research_candidates("fixture", r.id, 1)[0]["name"] == "b_mid"
