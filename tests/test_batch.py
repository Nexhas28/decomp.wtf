"""Compile-once ROM batches (plan P2.2, tests C-01/C-02): attempt_many compiles the translation unit
once per compatible group and evaluates each function against its own target through the same judge
as attempt(). Stub-based: no compiler needed."""

from __future__ import annotations

import json

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.network import NetworkError


class FakeResult:
    ok = True
    log = ""
    obj = b"batch"


class FakeCand:
    def __init__(self, name, fingerprint):
        self.name, self._fp = name, fingerprint

    def fingerprint(self):
        return self._fp


@pytest.fixture
def net(tmp_path):
    n = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    with n.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'fixture', 'fixture', 'batch fixture', 'mips', 0, '/tmp/fixture', "
                   "'{\"kind\": \"rom\", \"compiler\": {\"toolchain\": \"gcc-2.8.1-psx\", \"flags\": [\"-O2\"]}}')")
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, 'int func_xxx(void);', NULL, 'fixture', 0, '')")
        for i in range(1, 7):  # hash-only targets: the judge is the fingerprint comparison
            db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, seed, status, "
                       "best_score, best_source, best_agent_id, attempts, abandons, fingerprint, unit, vram, rom_offset) "
                       "VALUES(?,1,?,4,1,NULL,'[]','',NULL,'open',NULL,NULL,NULL,0,0,?,'',?,?)",
                       (i, f"func_00{i}", f"fp-{i}", 0x80001000 + 16 * i, 16 * i))
    return n


@pytest.fixture
def alice(net):
    return net.authenticate(net.register_agent("alice"))


@pytest.fixture
def stubs(net):
    calls = []

    class Broken:
        ok = False
        log = "batch fixture: deterministic compile error"
        obj = None

    net._compile = lambda proj, header, source, fn=None: (calls.append(1) or FakeResult())
    net._evaluate = lambda fn, proj, obj: (FakeCand(fn["name"], fn["fingerprint"]), None, [])
    return {"calls": calls, "Broken": Broken}


def test_c01_batch_compiles_once_and_verdicts_are_independent(net, alice, stubs):
    """Six compatible ROM functions in one source: one compile, six independent evaluations and
    acceptances, per-function receipts and epoch bumps (plan C-01)."""
    out = net.attempt_many(alice, [f"func_00{i}" for i in range(1, 7)],
                           "\n".join(f"int func_00{i}(void) {{ return {i}; }}" for i in range(1, 7)))
    assert "6 of 6 matched (1 compile for 6 functions)" in out
    assert len(stubs["calls"]) == 1
    assert net.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=1 AND status='matched'")["n"] == 6
    assert net.store.one("SELECT COUNT(*) AS n FROM verification_receipts WHERE function_id IN "
                         "(SELECT id FROM functions WHERE project_id=1)")["n"] == 6
    assert net.store.one("SELECT matched_epoch FROM projects WHERE id=1")["matched_epoch"] == 6
    assert net.store.one("SELECT COUNT(*) AS n FROM claims WHERE function_id IN "
                         "(SELECT id FROM functions WHERE project_id=1)")["n"] == 0  # every claim released


def test_c01b_shared_compile_error_reported_per_function(net, alice, stubs):
    net._compile = lambda proj, header, source, fn=None: stubs["Broken"]()
    out = net.attempt_many(alice, [f"func_00{i}" for i in range(1, 7)], "int x;")
    assert out.count("❌ COMPILE ERROR") == 6
    assert net.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=1 AND status='matched'")["n"] == 0


def test_c02_incompatible_contexts_partition(net, alice, stubs):
    """Functions whose units compile against different headers must not share a compile (plan C-02)."""
    with net.store.tx() as db:
        db.execute("UPDATE functions SET unit='unit_b' WHERE id >= 4")
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 2, 'int func_xxx(void); int unit_b_only;', NULL, 'fixture', 0, 'unit_b')")
    out = net.attempt_many(alice, [f"func_00{i}" for i in range(1, 7)],
                           "\n".join(f"int func_00{i}(void) {{ return {i}; }}" for i in range(1, 7)))
    assert "6 of 6 matched (2 compiles for 6 functions)" in out
    assert len(stubs["calls"]) == 2


def test_c02c_different_compiler_profiles_never_share_a_compile(net, alice, stubs):
    """Plan P2.2: the group key is the complete compilation identity, so a function pinned to another
    compiler profile gets its own compile, called with that function so its profile is used."""
    with net.store.tx() as db:
        cfg = json.loads(db.execute("SELECT config FROM projects WHERE id=1").fetchone()["config"])
        cfg["compiler_profiles"] = {"other": {"toolchain": "gcc-2.8.1-psx", "flags": ["-O1"]}}
        cfg["compiler_functions"] = {"func_002": "other"}
        db.execute("UPDATE projects SET config=? WHERE id=1", (json.dumps(cfg),))
    seen = []
    net._compile = lambda proj, header, source, fn=None: (seen.append(fn["name"] if fn is not None else None)
                                                          or FakeResult())
    out = net.attempt_many(alice, ["func_001", "func_002", "func_003"],
                           "\n".join(f"int func_00{i}(void) {{ return {i}; }}" for i in range(1, 4)))
    assert "3 of 3 matched (2 compiles for 3 functions)" in out
    assert sorted(seen) == ["func_001", "func_002"]  # one compile per profile, each given its function


def test_c02d_partial_claim_failure_releases_this_batchs_claims(net, alice, stubs):
    """A batch that fails part-way through claiming must not leave the claims it already took."""
    bob = net.authenticate(net.register_agent("bob"))
    net.claim(bob, function="func_003")
    with pytest.raises(NetworkError, match="claimed by bob"):
        net.attempt_many(alice, ["func_001", "func_002", "func_003"], "int x;")
    assert net.store.one("SELECT COUNT(*) AS n FROM claims WHERE agent_id=?", alice.id)["n"] == 0
    assert net.store.one("SELECT COUNT(*) AS n FROM claims WHERE function_id=3")["n"] == 1  # bob's is untouched
    assert stubs["calls"] == []


def test_c02b_batch_respects_claim_ownership(net, alice, stubs):
    bob = net.authenticate(net.register_agent("bob"))
    net.claim(bob, function="func_003")
    with pytest.raises(NetworkError, match="claimed by bob"):
        net.attempt_many(alice, ["func_001", "func_003"], "int x;")
