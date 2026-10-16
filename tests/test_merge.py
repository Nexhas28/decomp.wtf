"""C.4 boundary-merge execution (plan P8.6/PR-31): the reviewed reverse of split_function. Every
refusal path is covered; the ROM reassembly itself is delegated (stubbed here — it needs real ROM
fixtures and is exercised by the ROM lanes)."""

from __future__ import annotations

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError


@pytest.fixture
def net(tmp_path):
    n = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    with n.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'fixture', 'fixture', 'merge fixture', 'mips', 0, '/tmp/fixture', "
                   "'{\"kind\": \"rom\", \"compiler\": {\"toolchain\": \"gcc-2.8.1-psx\", \"flags\": [\"-O2\"]}}')")
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, 'int f(void);', NULL, 'fixture', 0, '')")
        # 1: head (0x1000, prologue) | 2: proper continuation of 1 (0x1010, prologue — NOT a fragment)
        # 3: unrelated (0x2000) | 4: a fragment-shaped tail continuing 2 (0x1020)
        db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, seed, status, "
                   "unit, vram, rom_offset) VALUES(1,1,'func_80001000',16,4,NULL,'[]','addiu $sp, $sp, -0x20',NULL,"
                   "'open','',0x80001000,0x10)")
        db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, seed, status, "
                   "unit, vram, rom_offset) VALUES(2,1,'func_80001010',16,4,NULL,'[]','addiu $sp, $sp, -0x20',NULL,"
                   "'open','',0x80001010,0x20)")
        db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, seed, status, "
                   "unit, vram, rom_offset) VALUES(3,1,'func_80002000',16,4,NULL,'[]','nop',NULL,"
                   "'open','',0x80002000,0x200)")
        db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, seed, status, "
                   "unit, vram, rom_offset) VALUES(4,1,'func_80001020',16,4,NULL,'[]','sb $s2, 0x10($a0)',NULL,"
                   "'open','',0x80001020,0x30)")
    return n


@pytest.fixture
def alice(net):
    return net.authenticate(net.register_agent("alice"))


def test_merge_requires_a_reviewed_proposal(net, alice):
    with pytest.raises(NetworkError, match="no reviewed merge proposal"):
        net.merge_function(alice, "func_80001020", "func_80001010")


def test_merge_requires_adjacency(net, alice):
    net.propose_merge("func_80001020", "func_80001010")
    with pytest.raises(NetworkError, match="not adjacent"):
        net.merge_function(alice, "func_80001020", "func_80001000")  # skips func_80001010


def test_merge_requires_the_fragment_shape(net, alice):
    # func_80001010 has a real prologue even though it is adjacent to func_80001000: not a fragment
    with net.store.tx() as db:
        db.execute("INSERT INTO merge_proposals(function_id, continues_id, at, evidence) VALUES(2,1,0,'x')")
    with pytest.raises(NetworkError, match="doesn't look like a fragment"):
        net.merge_function(alice, "func_80001010", "func_80001000")


def test_propose_merge_uses_the_same_direction_as_merge(net):
    """Both directions agree: the fragment is the tail (starts where the continuing function ends)."""
    assert "Merge proposed" in net.propose_merge("func_80001020", "func_80001010")
    with pytest.raises(NetworkError, match="not adjacent"):
        net.propose_merge("func_80001010", "func_80001020")  # reversed: the head can't continue the tail


def test_merge_never_touches_matched_functions(net, alice):
    net.propose_merge("func_80001020", "func_80001010")
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched' WHERE id=4")
    with pytest.raises(NetworkError, match="matched functions can't be merged"):
        net.merge_function(alice, "func_80001020", "func_80001010")


def test_merge_is_rom_only(net, alice):
    with net.store.tx() as db:
        db.execute("UPDATE projects SET config='{\"kind\": \"fork\"}' WHERE id=1")
    with pytest.raises(NetworkError, match="merge_function is for ROM projects"):
        net.merge_function(alice, "func_80001020", "func_80001010")


def test_merge_delegates_and_consumes_the_proposal(net, alice, monkeypatch):
    net.propose_merge("func_80001020", "func_80001010")
    calls = []

    def fake_merge(agent, proj, frag, cont):
        calls.append((frag["name"], cont["name"]))
        with net.store.tx() as db:
            net._consume_merge_proposals(db, frag["id"])
        return f"merged {frag['name']} into {cont['name']}"

    monkeypatch.setattr(net, "_merge_rom", fake_merge)
    out = net.merge_function(alice, "func_80001020", "func_80001010")
    assert "merged func_80001020 into func_80001010" in out
    assert calls == [("func_80001020", "func_80001010")]
    # the review evidence is void once the boundary no longer exists
    assert net._merge_proposal(4, 2) is None
    with pytest.raises(NetworkError, match="no reviewed merge proposal"):
        net.merge_function(alice, "func_80001020", "func_80001010")
