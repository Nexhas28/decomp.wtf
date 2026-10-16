"""Rooms: a host and a guest, each with their own copy of Demo Quest 64, working together."""

import json
import re
import shutil
import subprocess
import sys
import time

import pytest
from starlette.testclient import TestClient

from decompnet import compiler_profiles as profiles
from decompnet import toolchains
from decompnet.app import collab
from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError

from . import test_rom
from .test_app_engine import CLAMP, DAMAGE

DEMO_TOOLS = toolchains.installed("gcc-2.7.2-kmc") is not None and bool(shutil.which("ld.lld"))


@pytest.fixture(scope="module")
def built():
    """Demo Quest 64 (see test_rom): the tests that use it need gcc-2.7.2-kmc and ld.lld; the compiler-settings
    tests below don't."""
    if not DEMO_TOOLS:
        pytest.skip("needs gcc-2.7.2-kmc and ld.lld")
    if not test_rom.ROM.exists():
        subprocess.run([sys.executable, str(test_rom.DEMO / "build.py")], check=True)
    return test_rom.ROM.read_bytes()


@pytest.fixture(scope="module")
def room(built, tmp_path_factory):  # noqa: F811
    h = tmp_path_factory.mktemp("host")
    host = RomNetwork(h / "d.db", h)
    slug = host.import_rom(built, confirmed=True, log=lambda *_: None)
    host.rooms[slug] = collab.Room(host, slug)
    code = host.rooms[slug].open_invite()
    client = TestClient(collab.host_app(host, host.rooms), base_url="https://abc.trycloudflare.com")
    g = tmp_path_factory.mktemp("guest")
    guest = RomNetwork(g / "d.db", g)
    link = f"https://abc.trycloudflare.com/join/{code}#room={slug}"
    gslug = collab.join_room(guest, link, "alex", built, log=lambda *_: None, client=client, start=False)
    return host, slug, guest, gslug, client, code, link


def test_parse_invite():
    assert collab.parse_invite("https://x-y.trycloudflare.com/join/abcdefghij#room=demo") == ("https://x-y.trycloudflare.com", "abcdefghij#demo")
    with pytest.raises(NetworkError):
        collab.parse_invite("https://example.com/whatever")


def test_joining_needs_the_same_rom_and_a_valid_invite(room, built, tmp_path):  # noqa: F811
    host, slug, guest, gslug, client, code, link = room
    other = RomNetwork(tmp_path / "o.db", tmp_path)
    changed = bytearray(built)
    changed[0x2000] ^= 0xFF
    with pytest.raises(NetworkError, match="same as the host"):
        collab.join_room(other, link, "eve", bytes(changed), log=lambda *_: None, client=client, start=False)
    with pytest.raises(NetworkError, match="no longer valid"):
        collab.join_room(other, link.replace(code, "x" * 22), "eve", built, log=lambda *_: None, client=client, start=False)
    assert [m["name"] for m in host.rooms[slug].members()] == ["alex"]


def test_state_carries_work_never_rom_data(room):
    host, slug, guest, gslug, client, code, link = room
    tok = guest.links[gslug].token
    st = client.get(f"/collab/state?room={slug}", headers={"authorization": f"Bearer {tok}"}).json()
    text = json.dumps(st)
    assert "glabel" not in text and "target" not in st
    assert not re.search(r"/\* [0-9A-F]+ [0-9A-F]{8} [0-9A-F]{8} \*/", text)  # no disassembly lines
    assert st["functions"] and st["members"][0]["name"] == "alex"
    assert client.get(f"/collab/state?room={slug}", headers={"authorization": "Bearer dn_nope"}).status_code == 400


def test_split_and_match_from_the_guest_reach_the_host(room):
    host, slug, guest, gslug, client, code, link = room
    agent, _ = guest.local_agent("claude-1")
    assert guest.split_function(agent, gslug, "func_800007C4", "0x800007D0").startswith("✅")
    assert host._function("func_800007D0", slug)  # split on the host...
    assert guest._function("func_800007D0", gslug)  # ...and mirrored locally
    brief = guest.claim(agent, gslug, "func_800007D0")
    assert "glabel func_800007D0" in brief  # the guest's own copy provides the assembly
    claims = host.live(slug)["claims"]
    assert claims and claims[0]["agent"] == "alex.claude-1"
    out = guest.compile_and_diff(agent, "func_800007D0", CLAMP, gslug)
    assert out.startswith("✅ MATCH") and "host confirmed" in out
    assert host._function("func_800007D0", slug)["status"] == "matched"


def test_host_work_reaches_the_guest(room):
    host, slug, guest, gslug, client, code, link = room
    hagent, _ = host.local_agent("codex-1")
    assert host.compile_and_diff(hagent, "func_80000808", DAMAGE, slug).startswith("✅ MATCH")
    assert host.rename_symbol(hagent, slug, "func_800007D0", "clamp_s32").startswith("✅")
    assert host.edit_context(hagent, slug, "", "s32 clamp_s32(s32 v, s32 lo, s32 hi);", "prototype").startswith("✅")
    guest.links[gslug].sync()
    assert guest._function("func_80000808", gslug)["status"] == "matched"
    assert guest._function("clamp_s32", gslug)["status"] == "matched"
    assert "clamp_s32(s32 v" in guest.get_context(gslug)
    feed = [e["kind"] for e in guest.live(gslug)["events"]]
    assert "match" in feed
    assert guest.overview(gslug)["room"]["role"] == "guest"


def test_guest_edits_go_through_the_host(room):
    host, slug, guest, gslug, client, code, link = room
    agent, _ = guest.local_agent("claude-2")
    out = guest.rename_symbol(agent, gslug, "func_80000808", "player_take_damage")
    assert out.startswith("✅")
    assert host._function("player_take_damage", slug)["status"] == "matched"
    assert guest._function("player_take_damage", gslug)
    guest.add_note(agent, gslug, "player struct is at 0x8004AE34")
    assert "0x8004AE34" in host.search_notes(slug, "player struct")


ABS = "s32 func_800007F4(s32 v) { return v < 0 ? -v : v; }"


def test_two_friends_work_together(room, built, tmp_path):  # noqa: F811
    host, slug, guest, gslug, client, code, link = room
    bea = RomNetwork(tmp_path / "bea.db", tmp_path)
    bslug = collab.join_room(bea, link, "bea", built, log=lambda *_: None, client=client, start=False)
    assert sorted(m["name"] for m in host.rooms[slug].members()) == ["alex", "bea"]
    a_agent, _ = guest.local_agent("codex-1")
    b_agent, _ = bea.local_agent("claude-1")
    guest.claim(a_agent, gslug)
    bea.claim(b_agent, bslug)
    claims = {c["agent"]: c["name"] for c in host.live(slug)["claims"]}
    assert claims["alex.codex-1"] != claims["bea.claude-1"]  # the host never hands out the same function twice
    guest.release(a_agent, claims["alex.codex-1"], "", gslug)
    out = guest.compile_and_diff(a_agent, "func_800007F4", ABS, gslug)
    assert out.startswith("✅ MATCH"), out
    bea.links[bslug].sync()
    assert bea._function("func_800007F4", bslug)["status"] == "matched"  # bea sees alex's match


def test_reconnect_after_the_host_restarts(room, built):  # noqa: F811
    host, slug, guest, gslug, client, code, link = room
    host.rooms[slug].close()  # the host quits: the tunnel and its address are gone
    new_code = host.rooms[slug].open_invite()  # ...and shares again from a new address
    new_client = TestClient(collab.host_app(host, host.rooms), base_url="https://new-address.trycloudflare.com")
    new_link = f"https://new-address.trycloudflare.com/join/{new_code}#room={slug}"
    before = sorted(m["name"] for m in host.rooms[slug].members())
    again = collab.join_room(guest, new_link, "alex", b"", log=lambda *_: None, client=new_client, start=False)
    assert again == gslug
    assert sorted(m["name"] for m in host.rooms[slug].members()) == before  # same member, not "alex2"
    link_obj = guest.links[gslug]
    assert link_obj.url == "https://new-address.trycloudflare.com" and link_obj.member == "alex"
    link_obj.sync()
    agent, _ = guest.local_agent("claude-9")
    assert "CLAIMED" in guest.claim(agent, gslug)


def test_closing_the_room_locks_guests_out(room):
    host, slug, guest, gslug, client, code, link = room
    host.rooms[slug].close()
    with pytest.raises(NetworkError):
        collab.join_room(RomNetwork(":memory:", __import__("pathlib").Path("/tmp")), link, "late", b"", client=client, start=False)


def _host(built, tmp_path, **settings):  # noqa: F811
    host = RomNetwork(tmp_path / "h.db", tmp_path / "h")
    slug = host.import_rom(built, confirmed=True, log=lambda *_: None)
    room = collab.Room(host, slug)
    room.configure(**settings)
    host.rooms[slug] = room
    code = room.open_invite()
    client = TestClient(collab.host_app(host, host.rooms), base_url="http://192.168.1.20:6481")
    return host, slug, room, code, client


def _guest(tmp_path, name, link, built, client):  # noqa: F811
    g = RomNetwork(tmp_path / f"{name}.db", tmp_path / name)
    return g, collab.join_room(g, link, name, built, log=lambda *_: None, client=client, start=False)


def test_public_rooms_need_no_invite_code(built, tmp_path):  # noqa: F811
    host, slug, room, code, client = _host(built, tmp_path, visibility="public")
    g, gslug = _guest(tmp_path, "pub", f"http://192.168.1.20:6481/join/open#room={slug}", built, client)
    assert [m["name"] for m in room.members()] == ["pub"]
    room.configure(visibility="private")  # back to private: the bare address isn't enough any more
    with pytest.raises(NetworkError, match="no longer valid"):
        _guest(tmp_path, "late", f"http://192.168.1.20:6481/join/open#room={slug}", built, client)


def test_the_host_lets_people_in_and_can_take_them_out(built, tmp_path):  # noqa: F811
    host, slug, room, code, client = _host(built, tmp_path, approve=True)
    link = f"http://192.168.1.20:6481/join/{code}#room={slug}"
    g, gslug = _guest(tmp_path, "kim", link, built, client)
    assert [p["name"] for p in room.pending()] == ["kim"] and room.members() == []
    with pytest.raises(NetworkError, match="waiting for the host"):
        g.links[gslug].sync()
    room.approve("kim")
    g.links[gslug].sync()
    assert [m["name"] for m in room.members()] == ["kim"]
    room.remove("kim")
    with pytest.raises(NetworkError):
        g.links[gslug].sync()
    assert room.members() == []


def test_better_attempts_reach_the_host_without_a_compile_there(built, tmp_path):  # noqa: F811
    host, slug, room, code, client = _host(built, tmp_path)
    g, gslug = _guest(tmp_path, "lee", f"http://192.168.1.20:6481/join/{code}#room={slug}", built, client)
    agent, _ = g.local_agent("claude-1")
    g.claim(agent, gslug, "func_800007F4")
    res = g.attempt(agent, "func_800007F4", "s32 func_800007F4(s32 v) { return v + 1; }", gslug)
    assert res["score"] and not res["matched"]
    for _ in range(50):  # reported from a background thread
        row = host.store.one("SELECT best_score, best_source FROM functions WHERE name='func_800007F4'")
        if row["best_score"] is not None:
            break
        __import__("time").sleep(0.1)
    assert row["best_score"] == res["score"] and "v + 1" in row["best_source"]
    assert host.store.one("SELECT via FROM attempts ORDER BY id DESC LIMIT 1")["via"] == "room"


def test_a_crowd_in_one_room(built, tmp_path):  # noqa: F811
    host, slug, room, code, client = _host(built, tmp_path, visibility="public")
    link = f"http://192.168.1.20:6481/join/open#room={slug}"
    guests = [_guest(tmp_path, f"m{i}", link, built, client) for i in range(12)]
    agents = [g.local_agent("claude-1")[0] for g, _ in guests]
    claimed = [g.claim(a, s).splitlines()[0] for (g, s), a in zip(guests, agents)]
    assert len(set(claimed)) == len(claimed)  # never the same function twice
    members = room._members_now()  # members() is cached for a few seconds: everyone polls it
    assert len(members) == 12 and all(m["working"] for m in members)


def test_members_stay_in_the_room_when_their_app_restarts(built, tmp_path):  # noqa: F811
    host, slug, room, code, client = _host(built, tmp_path)
    g, gslug = _guest(tmp_path, "sam", f"http://192.168.1.20:6481/join/{code}#room={slug}", built, client)
    again = RomNetwork(tmp_path / "sam.db", tmp_path / "sam")  # the member's app starts again
    again.restore_links()
    assert gslug in again.links and again.links[gslug].member == "sam"
    again.links[gslug].client = client
    again.links[gslug].stop()
    again.links[gslug].sync()
    assert [m["name"] for m in room._members_now()] == ["sam"]


# -- compiler settings: members compile with exactly the host's (no ROM or compile needed)

HOST_COMPILER = {"toolchain": "gcc-2.7.2-psx", "flags": ["-O2", "-G0"], "wrapper": "cc", "timeout": 20,
                 "provenance": "explicit", "include_dirs": ["/host/only/include"]}
HOST_ASSIGNMENTS = {"compiler_profiles": {"ovl": {"toolchain": "gcc-2.8.1-psx", "flags": ["-O1", "-G0"]}},
                    "compiler_ranges": [{"start": 0x80100000, "end": 0x80200000, "profile": "ovl", "segment": "ovl_shop"}],
                    "compiler_functions": {"func_80010000": "ovl"}}


def _rom_project(net, root, compiler: dict, **extra) -> str:
    cfg = {"kind": "rom", "platform": "psx", "segments": [{"name": "main"}, {"name": "ovl_shop"}],
           "rom": {"sha1": "ab" * 20, "title": "TEST", "game_code": "SLUS-00000", "region": "USA"},
           "compiler": compiler, **extra}
    root.mkdir(parents=True, exist_ok=True)
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,root,config) VALUES(?,?,?,?)",
                         ("test", "TEST", str(root), json.dumps(cfg))).lastrowid
        for name, vram, unit in (("func_80010000", 0x80010000, "main"), ("shop_open", 0x80100000, "ovl_shop")):
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,seed,unit,vram) VALUES(?,?,8,2,?,?,?)",
                       (pid, name, "/* old draft */", unit, vram))
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,1,'',NULL,'basic types',?,'')", (pid, time.time()))
    return "test"


@pytest.fixture
def pair(tmp_path):
    """A host whose compiler settings were tuned over time, and a member who joined with only toolchain and flags."""
    host = RomNetwork(tmp_path / "h.db", tmp_path / "h")
    slug = _rom_project(host, tmp_path / "h" / "test", HOST_COMPILER, **HOST_ASSIGNMENTS)
    room = host.rooms[slug] = collab.Room(host, slug)
    code = room.open_invite()
    client = TestClient(collab.host_app(host, host.rooms), base_url="http://192.168.1.20:6481")
    joined = client.post(f"/collab/join?room={slug}", json={"invite": code, "name": "kai", "sha1": "ab" * 20}).json()
    guest = RomNetwork(tmp_path / "g.db", tmp_path / "g")
    _rom_project(guest, tmp_path / "g" / "test", {"toolchain": "gcc-2.7.2-psx", "flags": ["-O2"], "timeout": 20,
                                                   "sandbox": "member-only"})
    link = guest.link_project("test", {"url": "http://192.168.1.20:6481", "room": slug, "token": joined["token"],
                                       "member": joined["name"]}, client=client, start=False)
    return host, guest, link, client, code


def _cfg(net):
    return json.loads(net.project_row("test")["config"])


def _updates(net):
    return [e["detail"] for e in net.store.all("SELECT detail FROM events WHERE kind='collab'")
            if e["detail"].startswith("compiler settings updated")]


def test_members_compile_with_the_host_compiler_settings(pair):
    host, guest, link, client, code = pair
    link.sync()
    mine = _cfg(guest)
    assert profiles.shared(mine) == profiles.shared(_cfg(host))
    assert mine["compiler_ranges"] == HOST_ASSIGNMENTS["compiler_ranges"] and mine["compiler"]["flags"] == ["-O2", "-G0"]
    assert "include_dirs" not in mine["compiler"]  # the host's machine-local keys stay on the host...
    assert mine["compiler"]["sandbox"] == "member-only"  # ...and the member keeps its own
    assert guest.store.one("SELECT COUNT(*) AS n FROM functions WHERE seed IS NOT NULL")["n"] == 0  # old drafts dropped
    assert len(_updates(guest)) == 1 and "gcc-2.7.2-psx -O2 -G0" in _updates(guest)[0]
    link.sync()  # nothing changed: nothing rewritten, no second note in the feed
    assert len(_updates(guest)) == 1


def test_host_changes_reach_members_on_the_next_sync(pair):
    host, guest, link, client, code = pair
    link.sync()
    host.set_compiler("test", "gcc-2.7.2-psx", ["-O2", "-G8"], compiler_functions={},
                      compiler_ranges=[{"start": 0x80100000, "end": 0x80300000, "profile": "ovl"}])
    link.sync()
    mine = _cfg(guest)
    assert mine["compiler"]["flags"] == ["-O2", "-G8"] and "compiler_functions" not in mine
    assert mine["compiler_ranges"] == [{"start": 0x80100000, "end": 0x80300000, "profile": "ovl"}]
    assert profiles.shared(mine) == profiles.shared(_cfg(host)) and len(_updates(guest)) == 2


def test_a_member_cannot_change_the_compiler_settings(pair):
    host, guest, link, client, code = pair
    link.sync()
    with pytest.raises(NetworkError, match="follows the room host's compiler settings"):
        guest.set_compiler("test", "gcc-2.8.1-psx")
    with pytest.raises(NetworkError, match="follows the room host's compiler settings"):
        guest.set_compiler("test", compiler_functions={})
    guest.links.clear()  # the app restarted and the link isn't running yet: still refused
    with pytest.raises(NetworkError, match="follows the room host's compiler settings"):
        guest.set_compiler("test", "gcc-2.8.1-psx")
    host.set_compiler("test", "gcc-2.8.1-psx")  # the host itself can


def test_a_reconnect_or_local_drift_brings_the_host_settings_back(pair):
    host, guest, link, client, code = pair
    link.sync()
    cfg = _cfg(guest)
    cfg["compiler"]["flags"] = ["-O3"]  # e.g. edited outside the app
    with guest.store.tx() as db:
        db.execute("UPDATE projects SET config=? WHERE slug='test'", (json.dumps(cfg),))
    fresh = collab.Link(guest, "test", link.url, link.room, link.token, link.member, client=client)  # cursors at 0
    fresh.sync()
    assert _cfg(guest)["compiler"]["flags"] == ["-O2", "-G0"]


def test_hello_carries_the_settings_but_nothing_machine_local(pair):
    host, guest, link, client, code = pair
    hello = client.get("/collab/hello", params={"invite": code, "room": "test"}).json()
    assert hello["compiler"]["toolchain"] == "gcc-2.7.2-psx" and "include_dirs" not in hello["compiler"]
    assert hello["compiler_settings"] == profiles.shared(_cfg(host))


def test_an_old_host_or_an_unknown_compiler(pair):
    host, guest, link, client, code = pair
    before = _cfg(guest)
    link.apply({"e": 0, "events": [], "claims": [], "members": []})  # an older host sends no settings
    assert _cfg(guest) == before
    cfg = _cfg(host)
    cfg["compiler"]["toolchain"] = "gcc-from-the-future"  # a compiler this member's version doesn't know
    with host.store.tx() as db:
        db.execute("UPDATE projects SET config=? WHERE slug='test'", (json.dumps(cfg),))
    with pytest.raises(NetworkError, match="can't use the room host's compiler settings"):
        link.sync()
    assert "gcc-from-the-future" in link.error and _cfg(guest) == before  # loud, and nothing half-applied


# -- the room's host is the judge (an older host that sends no compiler settings: its verdicts are stubbed)

@pytest.fixture
def judged(pair, monkeypatch):
    """The member's compiler and scorer stubbed: a source with MATCHES in it is exact here, anything else scores
    40. `host` holds what the stubbed host answers to /collab/attempt (an exception: unreachable)."""
    from types import SimpleNamespace

    from decompnet import scoring, validity

    host, guest, link, client, code = pair
    with guest.store.tx() as db:
        db.execute("UPDATE functions SET target=? WHERE project_id=1", (b"\0" * 8,))
    monkeypatch.setattr(guest, "_compile", lambda proj, header, source, fn=None: SimpleNamespace(ok=True, obj=source, log=""))
    monkeypatch.setattr(guest, "_evaluate", lambda fn, proj, obj: (SimpleNamespace(fingerprint=lambda: "fp", src=obj), None, []))
    monkeypatch.setattr(guest, "_target_obj", lambda fn, proj: None)
    monkeypatch.setattr(guest, "_validity", lambda fn, source: validity.Validity(ok=True))
    monkeypatch.setattr(guest, "_worth_variant", lambda verdict, d: False)
    for quiet in ("_evidence", "_permuter_handoff"):
        monkeypatch.setattr(guest, quiet, lambda *a, **k: "")
    real_diff = scoring.diff
    monkeypatch.setattr(scoring, "diff", lambda target, cand: real_diff(target, cand) if target is not None else
                        scoring.DiffResult(0 if "MATCHES" in cand.src else 40, 100, [], "MATCHES" in cand.src))
    answer, asked = {"host": {"score": 230, "matched": False}}, []

    def attempt(agent, function, source):
        asked.append((function, source))
        if isinstance(answer["host"], Exception):
            raise answer["host"]
        return answer["host"]
    monkeypatch.setattr(link, "attempt", attempt)
    monkeypatch.setattr(link, "call", lambda *a, **k: "")
    agent, _ = guest.local_agent("claude-1")
    return guest, link, agent, answer, asked


def _fn(guest, name="shop_open"):
    return guest.store.one("SELECT * FROM functions WHERE name=?", name)


def test_a_match_the_host_rejects_stays_open(judged):
    guest, link, agent, answer, asked = judged
    res = guest.attempt(agent, "shop_open", "void shop_open(void) {} /* MATCHES */", "test")
    assert not res["matched"] and res["score"] == 230 and "host scored 230" in res["text"]
    assert "decomp-assembler" in res["text"] and asked == [("shop_open", "void shop_open(void) {} /* MATCHES */")]
    fn = _fn(guest)
    assert fn["status"] == "open" and fn["matched_source"] is None and fn["best_score"] == 230
    assert "MATCHES" in fn["best_source"]
    assert guest.store.one("SELECT COUNT(*) AS n FROM verification_receipts")["n"] == 0
    assert guest.store.one("SELECT COUNT(*) AS n FROM events WHERE kind='match'")["n"] == 0
    assert guest.store.one("SELECT agent_id FROM claims WHERE function_id=?", fn["id"])["agent_id"] == agent.id


def test_a_match_the_host_confirms_is_kept(judged):
    guest, link, agent, answer, asked = judged
    answer["host"] = {"score": 0, "matched": True}
    res = guest.attempt(agent, "shop_open", "void shop_open(void) {} /* MATCHES */", "test")
    assert res["matched"] and "host confirmed" in res["text"] and len(asked) == 1
    assert _fn(guest)["status"] == "matched"


def test_an_unreachable_host_saves_no_match(judged):
    guest, link, agent, answer, asked = judged
    answer["host"] = NetworkError("the room's host isn't reachable right now")
    res = guest.attempt(agent, "shop_open", "void shop_open(void) {} /* MATCHES */", "test")
    assert not res["matched"] and "NOT saved as a match" in res["text"]
    fn = _fn(guest)
    assert fn["status"] == "open" and fn["best_score"] is None  # nothing pretends to be a 0


def test_sources_with_compiler_comments_are_judged_by_the_host(judged):
    guest, link, agent, answer, asked = judged
    src = "/* decomp-assembler: maspsx-2.79-div */\nvoid shop_open(void) {}"
    answer["host"] = {"score": 12, "matched": False}
    res = guest.attempt(agent, "shop_open", src, "test")
    assert res["score"] == 12 and not res["matched"] and "only the host applies" in res["text"]
    assert _fn(guest)["best_score"] == 12 and _last_attempt(guest) == 12  # the host's score, everywhere
    answer["host"] = {"score": 0, "matched": True}  # this copy can't match it, the host can
    res = guest.attempt(agent, "shop_open", src + " ", "test")
    assert res["matched"] and _fn(guest)["status"] == "matched" and _fn(guest)["matched_source"] == src + " "
    plain = guest.attempt(agent, "func_80010000", "void func_80010000(void) {}", "test")  # no comments: not asked
    assert plain["score"] == 40 and [f for f, _ in asked] == ["shop_open", "shop_open"]


def test_the_first_sync_reopens_matches_the_host_never_confirmed(pair):
    host, guest, link, client, code = pair
    remote = guest._remote_agent("bea.claude-1")
    local, _ = guest.local_agent("claude-1")
    with guest.store.tx() as db:  # an older copy accepted these locally; the host has none of them
        db.execute("UPDATE functions SET status='matched', matched_source='int a;', matched_agent_id=? "
                   "WHERE name='shop_open'", (local.id,))
        db.execute("UPDATE functions SET status='matched', matched_source='int b;', matched_agent_id=? "
                   "WHERE name='func_80010000'", (remote,))
    with host.store.tx() as db:  # ...except that the host matched func_80010000 itself
        db.execute("UPDATE functions SET status='matched', matched_source='int b;' WHERE name='func_80010000'")
    link.sync()
    fn = _fn(guest)
    assert fn["status"] == "open" and fn["best_source"] == "int a;" and fn["matched_source"] is None
    assert _fn(guest, "func_80010000")["status"] == "matched"  # mirrored from the room: never touched
    assert any("reopened 1 match" in e["detail"] for e in guest.store.all("SELECT detail FROM events WHERE kind='collab'"))
    with guest.store.tx() as db:
        db.execute("UPDATE functions SET status='matched', matched_source='int a;', matched_agent_id=? "
                   "WHERE name='shop_open'", (local.id,))
    link.sync()  # later syncs leave matches alone: only the first one carries the host's full list
    assert _fn(guest)["status"] == "matched"


def _last_attempt(guest):
    return guest.store.one("SELECT score FROM attempts ORDER BY id DESC LIMIT 1")["score"]


def test_one_attempt_asks_the_host_once(judged):
    """Exact here AND carrying compiler comments: both reasons to ask the host, still one question, and the
    reply, the stored attempt and the best all show the host's score."""
    guest, link, agent, answer, asked = judged
    answer["host"] = {"score": 760, "matched": False}
    src = "/* decomp-compiler: gcc-2.7.2-psx */ /* decomp-cflags: -O2 */ void shop_open(void) {} /* MATCHES */"
    res = guest.attempt(agent, "shop_open", src, "test")
    assert len(asked) == 1 and asked[0][1] == src  # sent as written: no prelude, nothing stripped
    assert res["score"] == 760 and not res["matched"]
    assert _last_attempt(guest) == 760 and _fn(guest)["best_score"] == 760 and _fn(guest)["status"] == "open"
    answer["host"] = {"score": 0, "matched": True}
    res = guest.attempt(agent, "shop_open", src + " ", "test")
    assert res["matched"] and len(asked) == 2 and _fn(guest)["status"] == "matched"
