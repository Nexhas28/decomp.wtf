"""PR-31: wall-aware claiming, structured wall registry, legacy best revalidation, merge proposals.
Field-driven by perf-test's production run (stale bests, wall-saturated pool, boundary fragments).
Stub-based: no compiler needed."""

from __future__ import annotations

import time

import pytest

from decompnet import snapshots
from decompnet.network import (Network, NetworkError, WALL_COOLDOWN, _fragment_like, wall_signature)

from .test_permute import make_project


@pytest.fixture
def net(tmp_path):
    n = Network(tmp_path / "w.db")
    make_project(n, 6)
    return n


@pytest.fixture
def alice(net):
    return net.authenticate(net.register_agent("alice"))


def claim_name(net, agent, strategy="easiest"):
    return net.claim(agent, strategy=strategy).splitlines()[0].split("/")[-1]


def test_wall_signature_classes_notes():
    assert wall_signature("GTE/COP2 code: lwc2/swc2 present") == "gte-cop2"
    assert wall_signature("Illegal operands lui/addiu on jtbl_ labels") == "jump-table"
    assert wall_signature("extra nop before jal; load-delay") == "load-delay-nop"
    assert wall_signature("BIOS jalr veneer t2=0xB0") == "bios-veneer"
    assert wall_signature("false-split continuation of func_X: live-in $s2") == "boundary-fragment"
    assert wall_signature("reproducible floor 2845 under gcc-2.8.1") == "toolchain-floor"
    assert wall_signature("pure s2/s3 allocno swap") == "regalloc-tie"
    assert wall_signature("tried casts; didn't work") is None


def test_release_with_wall_note_records_and_schedules_out(net, alice):
    name = claim_name(net, alice)
    out = net.release(alice, name, "jump-table: Illegal operands lui/addiu on jtbl_ labels")
    assert "Wall recorded (jump-table)" in out
    row = net.store.one("SELECT * FROM function_walls WHERE function_id="
                        "(SELECT id FROM functions WHERE name=?)", name)
    assert row is not None and row["signature"] == "jump-table"
    budget = net.store.one("SELECT cooldown_until, reset_reason FROM search_budgets WHERE function_id="
                           "(SELECT id FROM functions WHERE name=?)", name)
    assert budget["reset_reason"] == "wall:jump-table" and budget["cooldown_until"] > time.time()
    # strategy claims skip it now...
    assert claim_name(net, net.authenticate(net.register_agent("bob"))) != name
    # ...but claiming it BY NAME still works (the agent knows what it's doing)
    assert name in net.claim(net.authenticate(net.register_agent("carol")), function=name)


def test_context_change_clears_wall_cooldown(net, alice):
    name = claim_name(net, alice)
    net.release(alice, name, "regalloc tie: allocno swap exhausted")
    fid = net.store.one("SELECT id FROM functions WHERE name=?", name)["id"]
    assert net.store.one("SELECT cooldown_until FROM search_budgets WHERE function_id=?", fid)["cooldown_until"] > time.time()
    with net.store.tx() as db:  # the commit-time reset (edit_context calls this; compile-free here)
        net._reset_budgets_for_context(db, 1, "", 2)
    assert net.store.one("SELECT cooldown_until FROM search_budgets WHERE function_id=?", fid)["cooldown_until"] == 0


def test_ordinary_release_keeps_claimable(net, alice):
    name = claim_name(net, alice)
    net.release(alice, name, "tried casts; didn't work")
    assert net.store.one("SELECT * FROM function_walls WHERE function_id="
                         "(SELECT id FROM functions WHERE name=?)", name) is None


def test_fragment_detection():
    row = {"asm": "\tori $t0, $t0, 1\n\tjr $ra\n", "n_insns": 2}
    assert _fragment_like(row) is False  # no saved register
    row = {"asm": "\tsb $s2, 0x10($a0)\n\tjr $ra\n", "n_insns": 2}
    assert _fragment_like(row) is True  # live-in $s2, no prologue
    row = {"asm": "\taddiu $sp, $sp, -0x20\n\tsb $s2, 0x10($a0)\n", "n_insns": 2}
    assert _fragment_like(row) is False  # real prologue


def test_propose_merge_validates_and_records(net, alice):
    with net.store.tx() as db:
        db.execute("UPDATE functions SET vram=0x80001000, rom_offset=0x10, size=16 WHERE id=1")
        db.execute("UPDATE functions SET vram=0x80001010, rom_offset=0x20, size=16 WHERE id=2")
        db.execute("UPDATE functions SET vram=0x80001020, rom_offset=0x30, size=16 WHERE id=3")
        db.execute("UPDATE functions SET asm='sb $s2, 0x10($a0)' WHERE id=3")  # the tail fragment
        db.execute("UPDATE functions SET asm='addiu $sp, $sp, -0x20' WHERE id=2")  # real prologue
    out = net.propose_merge("func_003", "func_002")  # the fragment is the tail (same direction as merge)
    assert "Merge proposed" in out
    row = net.store.one("SELECT * FROM merge_proposals")
    assert row is not None and row["continues_id"] == 2
    brief = net.get_function("func_003")
    assert "Boundary merge proposals" in brief and "continues func_002" in brief
    with pytest.raises(NetworkError, match="not adjacent"):
        net.propose_merge("func_003", "func_001")  # skips func_002
    with pytest.raises(NetworkError, match="doesn't look like a fragment"):
        net.propose_merge("func_002", "func_001")  # adjacent, but func_002 has a prologue


def test_legacy_best_marked_unverified_and_revalidated(net, alice, monkeypatch):
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    with net.store.tx() as db:
        db.execute("UPDATE functions SET best_score=NULL, best_source=NULL WHERE id=1")
    # a pre-snapshot best: the attempt row has no attempt_snapshots row (legacy)
    net._record(fn, alice, 50, "int func_001(void) { return 1; }", 1, snapshot=None)
    assert net.mark_legacy_unverified("fixture") >= 1
    assert net.store.one("SELECT best_verified FROM functions WHERE id=1")["best_verified"] == 0
    brief = net.get_function("func_001")
    assert "LEGACY BEST (unverified)" in brief
    # a snapshot-backed improvement flips it back to verified
    net._record(fn, alice, 40, "int func_001(void) { return 1; }", 1,
                snapshot=snapshots.capture(fn, proj, net._context(1, "")))
    assert net.store.one("SELECT best_verified FROM functions WHERE id=1")["best_verified"] == 1

    # revalidation: a legacy best that recompiles to the same score verifies
    fn3 = net.store.one("SELECT * FROM functions WHERE id=3")
    with net.store.tx() as db:
        db.execute("UPDATE functions SET best_verified=0 WHERE id=3")

    class FakeResult:
        ok, log, obj = True, "", b"x"

    class FakeCand:
        def fingerprint(self):
            return "fp"

    monkeypatch.setattr(net, "_compile", lambda proj, header, source: FakeResult())
    monkeypatch.setattr(net, "_evaluate", lambda fn, proj, obj: (FakeCand(), None, []))
    with net.store.tx() as db:
        db.execute("UPDATE functions SET target=NULL, fingerprint='fp' WHERE id=3")
    fn3 = net.store.one("SELECT * FROM functions WHERE id=3")
    out = net.revalidate_bests("fixture", limit=10)
    assert "func_003" in out
    assert net.store.one("SELECT best_verified FROM functions WHERE id=3")["best_verified"] == 1
