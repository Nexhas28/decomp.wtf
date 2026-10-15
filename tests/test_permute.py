"""Queue, lease and budget correctness for the background permuter (plan Q-01..Q-06).

These tests run without any compiler: they exercise the store/claim/budget logic directly.
"""

from __future__ import annotations

import time

import pytest

from decompnet.app.permute import MAX_TURNS, Permuter
from decompnet.network import Network


def make_project(net: Network, n_functions: int, near: float = 0.06) -> dict:
    """A synthetic project with `n_functions` open near-miss functions. Function i (1-based) has
    best_score = i, so the permuter's near-miss ordering (best_score / (n_insns * 100)) ranks
    lower numbers first."""
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'fixture', 'fixture', 'queue fixture', 'mips', 0, '/tmp/fixture', '{}')")
        for i in range(1, n_functions + 1):
            db.execute(
                "INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, seed, status, "
                "best_score, best_source, best_agent_id, attempts, abandons) VALUES(?,1,?,?,100,NULL,'[]','',NULL,"
                "'open',?,?,NULL,0,0)",
                (i, f"func_{i:03d}", 400, i, f"/* best attempt {i} */\nint func_{i:03d}(void) {{ return {i}; }}"),
            )
    return net.store.one("SELECT * FROM projects WHERE id=1")


def exhaust(net: Network, ids: range, turns: int = MAX_TURNS) -> None:
    """Mark functions as having spent their permuter turn budget."""
    now = time.time()
    with net.store.tx() as db:
        for i in ids:
            db.execute("INSERT INTO search_budgets(function_id, turns_used, max_turns, last_at) VALUES(?,?,?,?)",
                       (i, turns, MAX_TURNS, now))


@pytest.fixture
def net(tmp_path):
    return Network(tmp_path / "queue.db")


def test_q01_exhausted_functions_cannot_hide_remaining_work(net):
    """The top 80 near-misses have spent their budget; the next 20 are eligible. The permuter must
    return those 20 instead of an empty queue (plan Q-01: the reviewed query applies LIMIT before
    the Python turn filter, so limit*4 exhausted rows hid every eligible function)."""
    proj = make_project(net, 100)
    exhaust(net, range(1, 81))
    perm = Permuter(net)
    todo = perm.candidates(proj, limit=20)
    assert [f["name"] for f in todo] == [f"func_{i:03d}" for i in range(81, 101)]


def test_q01b_ordering_is_deterministic_and_stable(net):
    """The same candidate window is returned on every call: a stable ordering with a tiebreak
    (plan P1.1 steps 6 and 8), so the pagination fix advances instead of re-querying page one."""
    proj = make_project(net, 100)
    exhaust(net, range(1, 81))
    perm = Permuter(net)
    first = [f["id"] for f in perm.candidates(proj, limit=20)]
    second = [f["id"] for f in perm.candidates(proj, limit=20)]
    assert first == second
    assert first == list(range(81, 101))


def test_q01c_budget_is_persisted_across_restarts(net):
    """Exhaustion survives a restart: a fresh Permuter over the same database still sees the same
    eligible set (plan P1.1 step 4 and Q-05: budgets are database state, not an in-memory dict)."""
    proj = make_project(net, 100)
    exhaust(net, range(1, 81))
    Permuter(net).candidates(proj, limit=20)
    fresh = Permuter(Network(net.store.conn.execute("PRAGMA database_list").fetchone()["file"]))
    todo = fresh.candidates(proj, limit=20)
    assert [f["id"] for f in todo] == list(range(81, 101))


def test_q01d_zero_or_unknown_instruction_counts_are_never_ranked(net):
    """Functions with n_insns 0/NULL must not appear in the near-miss ranking (plan P1.1 step 5:
    guard the score normalisation basis)."""
    proj = make_project(net, 5)
    with net.store.tx() as db:
        db.execute("UPDATE functions SET n_insns=0 WHERE id=1")
        db.execute("UPDATE functions SET n_insns=NULL WHERE id=2")
    perm = Permuter(net)
    ids = [f["id"] for f in perm.candidates(proj, limit=20)]
    assert 1 not in ids and 2 not in ids
    assert ids == [3, 4, 5]


def test_q02_claimed_functions_are_not_candidates(net):
    """An open function another agent holds a claim on must not be offered to the permuter
    (plan Q-02's prerequisite: only open, unclaimed candidates are selected)."""
    proj = make_project(net, 5)
    with net.store.tx() as db:
        db.execute("INSERT INTO agents(id, name, owner, token_hash) VALUES(1, 'worker', NULL, 't1')")
        db.execute("INSERT INTO claims(function_id, agent_id, claimed_at, expires_at) VALUES(1, 1, 0, 9e18)")
    perm = Permuter(net)
    ids = [f["id"] for f in perm.candidates(proj, limit=20)]
    assert 1 not in ids
    assert ids == [2, 3, 4, 5]
