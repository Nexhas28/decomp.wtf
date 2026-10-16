"""Bounded grouped drafting (plan P4.5 / test D-06): caller/callee neighbourhoods drafted under
per-unit contexts, bounded by members and total instructions, recorded as 'm2c-group' candidates.
Stub-based: seeds.draft is faked, no m2c needed."""

from __future__ import annotations

import json

import pytest

from decompnet.app.engine import RomNetwork
from decompnet import seeds
from decompnet.network import NetworkError


@pytest.fixture
def net(tmp_path):
    n = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    with n.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'fixture', 'fixture', 'group fixture', 'mips', 0, '/tmp/fixture', "
                   "'{\"kind\": \"rom\", \"compiler\": {\"toolchain\": \"gcc-2.8.1-psx\", \"flags\": [\"-O2\"]}}')")
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, 'int f1(void); int f2(void); int f3(void);', NULL, 'fixture', 0, '')")
        # fn1 calls fn2 and fn3 (reloc [off, rtype, target-addr]); fn2/fn3 are plain functions
        rows = [
            (1, "func_80001000", 0x80001000, '[[0, 5, 2147487776], [0, 5, 2147487792]]'),  # calls 0x80001020, 0x80001030
            (2, "func_80001020", 0x80001020, '[]'),
            (3, "func_80001030", 0x80001030, '[]'),
            (4, "func_80001040", 0x80001040, '[]'),  # unrelated
        ]
        for fid, name, vram, relocs in rows:
            db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, seed, status, "
                       "best_score, best_source, best_agent_id, attempts, abandons, unit, vram, rom_offset) "
                       "VALUES(?,1,?,16,8,NULL,?,'',NULL,'open',NULL,NULL,NULL,0,0,'',?,0)", (fid, name, relocs, vram))
    return n


@pytest.fixture
def fake_draft(monkeypatch, net):
    """A deterministic seeds.draft that records (name, context header) and returns a distinct text."""
    calls = []

    def fake(asm_text, name, toolchain_id=None, *, context_header="", **kw):
        calls.append((name, context_header))
        return seeds.DraftResult(text=f"int {name}(void) {{ return 0; }}", outcome="ok", context_status="prepared")

    monkeypatch.setattr("decompnet.app.engine.seeds.draft", fake)
    return calls


def test_d06_bounded_neighbourhood_is_drafted_under_each_units_context(net, fake_draft):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    out = net.draft_group(proj, fn, max_members=5, max_insns=600)
    names = [c[0] for c in fake_draft]
    assert names == ["func_80001000", "func_80001020", "func_80001030"]  # the function + its direct callees
    assert "func_80001040" not in names  # unrelated neighbour never drafted
    assert "3 function(s)" in out
    # every draft is retained as a provenance-tagged candidate
    rows = net.store.all("SELECT origin FROM candidates ORDER BY id")
    assert [r["origin"] for r in rows] == ["m2c-group"] * 3


def test_d06b_member_and_instruction_bounds_hold(net, fake_draft):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    out = net.draft_group(proj, fn, max_members=2, max_insns=600)
    assert len(fake_draft) == 2 and "2 function(s)" in out
    out = net.draft_group(proj, fn, max_members=5, max_insns=15)  # fn1 (8) + fn2 (8) exceeds the budget
    assert "1 function(s)" in out


def test_d06c_unit_contexts_are_preserved_per_member(net, fake_draft):
    with net.store.tx() as db:
        db.execute("UPDATE functions SET unit='unit_b' WHERE id=2")
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 2, 'int f2b(void);', NULL, 'fixture', 0, 'unit_b')")
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    net.draft_group(proj, fn)
    headers = {name: h for name, h in fake_draft}
    assert headers["func_80001020"] == "int f2b(void);"  # member drafted against its own unit's header
    assert headers["func_80001000"].startswith("int f1(void);")


def test_d06d_no_m2c_is_reported_not_raised(net, monkeypatch):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    monkeypatch.setattr("decompnet.app.engine.seeds.available", lambda: False)
    assert "isn't installed" in net.draft_group(proj, fn)
