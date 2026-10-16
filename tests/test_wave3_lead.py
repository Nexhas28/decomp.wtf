"""Wave-3 Lead quick wins: claim-closest size cap (C.5) and the bridge identity split-brain fix
(C.6). Stub-based."""

from __future__ import annotations

import pytest

from decompnet.app.engine import RomNetwork

from .test_permute import make_project


@pytest.fixture
def net(tmp_path):
    n = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    make_project(n, 4)
    with n.store.tx() as db:
        db.execute("UPDATE functions SET n_insns=1000, best_score=5, best_source='x' WHERE id=1")  # a near-miss giant
        db.execute("UPDATE functions SET n_insns=100, best_score=200, best_source='y' WHERE id=2")
        db.execute("UPDATE functions SET best_score=8000 WHERE id IN (3,4)")  # farther near-misses
    return n


@pytest.fixture
def alice(net):
    return net.authenticate(net.register_agent("alice"))


def test_c5_closest_skips_giants_but_byname_and_hardest_still_reach_them(net, alice):
    a = alice
    name = net.claim(a, strategy="closest").splitlines()[0].split("/")[-1]
    assert name == "func_002"  # the 100-insn near-miss, never the 1000-insn giant
    net.release(a, name, "")
    bob = net.authenticate(net.register_agent("bob"))
    assert net.claim(bob, strategy="hardest").splitlines()[0].split("/")[-1] == "func_001"  # hardest is uncapped
    net.release(bob, "func_001", "")
    carol = net.authenticate(net.register_agent("carol"))
    assert "func_001" in net.claim(carol, function="func_001")  # by name always works


def test_c6_same_bridge_client_keeps_its_identity_across_restarts(net):
    a1, t1 = net.chat_agent("worker", client="bridge-a")
    assert a1.name == "worker"
    assert net.store.one("SELECT bridge FROM agents WHERE name='worker'")["bridge"] == "bridge-a"
    # a daemon restart with the same client id: same row, fresh token — no suffixed identity
    a2, t2 = net.chat_agent("worker", client="bridge-a")
    assert a2.name == "worker" and a2.id == a1.id and t2 != t1
    # a genuinely different concurrent client still gets its own identity
    a3, _ = net.chat_agent("worker", client="bridge-b")
    assert a3.name == "worker_2"
    # a client-less mint (legacy bridge) behaves as before: fresh session -> suffixed
    a4, _ = net.chat_agent("worker")
    assert a4.name in ("worker_2", "worker_3")
