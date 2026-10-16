"""A.7/A.10 harness tests: the statistics, the holdout-recall measurements and the grouped-vs-single
drafting comparison logic. Pure python — no compiler, no m2c, no live database."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent.parent / "benchmarks" / "harness"
sys.path.insert(0, str(HARNESS))

import stats  # noqa: E402
from stats import Arm, compare, wilson  # noqa: E402

import holdout_recall as hr  # noqa: E402
import draft_compare as dc  # noqa: E402

from decompnet.app.engine import RomNetwork  # noqa: E402

from .test_permute import make_project  # noqa: E402


# ------------------------------------------------------------------ stats

def test_wilson_interval_is_bounded_and_symmetric():
    assert wilson(0, 0) == (0.0, 1.0)          # no data: the whole range, honestly
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and 0 < hi < 0.35          # zero successes still has an upper bound
    lo2, hi2 = wilson(10, 10)
    assert 0.65 < lo2 < 1.0 and hi2 == 1.0
    lo3, hi3 = wilson(5, 10)
    assert lo3 < 0.5 < hi3 and 0 <= lo3 and hi3 <= 1


def test_arm_reports_rate_with_denominator_and_interval():
    a = Arm("guided")
    a.add(solved=True, improved=True, compiles=7, wall_s=1.5)
    a.add(solved=False, improved=True, compiles=3, wall_s=0.5)
    d = a.to_dict()
    assert d["trials"] == 2 and d["solved"] == 1 and d["improved"] == 2
    assert d["solved_rate"] == 0.5 and len(d["solved_rate_ci95"]) == 2
    assert d["compiles_per_trial"] == 5.0 and d["wall_s"] == 2.0


def test_compare_refuses_to_overclaim_on_overlapping_intervals():
    a, b = Arm("a"), Arm("b")
    for solved in (True, False, True, False, True, False, True, False, True, False):
        a.add(solved, False, 1, 0.1)
        b.add(not solved, False, 1, 0.1)
    assert compare([a, b])["verdict"] == "inconclusive"
    # a clear separation does get called
    c, d = Arm("c"), Arm("d")
    for _ in range(20):
        c.add(True, True, 1, 0.1)
        d.add(False, False, 1, 0.1)
    out = compare([c, d])
    assert out["verdict"] == "leads" and out["leader"] == "c"
    assert compare([c])["verdict"] == "no_data"


# ------------------------------------------------------------------ holdout recall

def test_recall_at_k_and_verified_reuse():
    answers = {"q1": {"target_digest": "t1"}, "q2": {"target_digest": "t2"}}
    cands = [{"target_digest": "t1"}, {"target_digest": "x"}]
    r = hr.recall_at_k(cands, answers, k=1)
    assert r["recalled"] == 1 and r["recall"] == 0.5 and r["answers"] == 2
    r2 = hr.recall_at_k(cands, answers, k=5)
    assert r2["recalled"] == 1  # t2 was never surfaced, at any k
    v = hr.verified_reuse_rate([{"surfaced": True, "verified": True},
                                {"surfaced": True, "verified": False},
                                {"surfaced": False, "verified": False}])
    assert v["surfaced"] == 2 and v["verified"] == 1 and v["verified_reuse_rate"] == 0.5
    assert hr.verified_reuse_rate([])["verified_reuse_rate"] is None


def test_leakage_refuses_a_contaminated_corpus(tmp_path):
    from decompnet.retrieval import ReferenceEntry, Registry

    reg = Registry()
    reg.add(ReferenceEntry(entry_id="e1", repository="lib", version="1", source_location="a.c",
                           toolchain="gcc", profile="p", target_identity="t1", source_digest="s1"))
    answers = {"q1": {"target_digest": "t1", "source_digest": "s1", "source_location": "a.c"}}
    assert hr.leakage(reg, answers), "an indexed copy of the answer must be reported as leakage"
    # a holdout-marked entry is correctly separated and is not a leak
    reg2 = Registry()
    reg2.add(ReferenceEntry(entry_id="e1", repository="lib", version="1", source_location="a.c",
                            toolchain="gcc", profile="p", target_identity="t1", source_digest="s1",
                            holdout=True))
    assert hr.leakage(reg2, answers) == []


# ------------------------------------------------------------------ grouped vs single drafting

@pytest.fixture
def net(tmp_path):
    n = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    make_project(n, 4)
    with n.store.tx() as db:
        db.execute("UPDATE functions SET vram=0x80001000, rom_offset=0x10 WHERE id=1")
        db.execute("UPDATE functions SET vram=0x80001010, rom_offset=0x20 WHERE id=2")
        db.execute("UPDATE functions SET relocs='[[0, 5, 2147487760]]' WHERE id=1")  # calls 0x80001010
    return n


def test_grouped_stub_counts_one_preparation_for_the_neighbourhood(net):
    proj = net.store.one("SELECT * FROM projects WHERE id=1")
    fn = net.store.one("SELECT * FROM functions WHERE id=1")
    g = dc.run_grouped(net, proj, fn, 4, stub=True)
    assert g["mode"] == "grouped" and g["drafts"] >= 1
    assert g["provider_calls"] == g["drafts"]  # one preparation, members drafted from it
    s = dc.run_single(net, proj, fn, stub=True)
    assert s["mode"] == "single" and s["drafts"] == 1 and s["provider_calls"] == 1


def test_draft_compare_reports_unavailable_without_m2c(monkeypatch, capsys):
    monkeypatch.setattr(dc.seeds, "available", lambda: False)
    monkeypatch.setattr(sys, "argv", ["draft_compare.py", "--db", "x", "--function", "f"])
    assert dc.main() == 3  # unavailable, never invented numbers
    out = capsys.readouterr().out
    assert "unavailable" in out and "m2c" in out
