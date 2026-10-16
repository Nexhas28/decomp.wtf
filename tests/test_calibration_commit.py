"""Calibrate-and-commit (plan P4.1.7) and the engine wiring of profile provenance and
invalidation events (A.5): held-out validation gates every commit, guesses stay hypotheses,
and a profile identity change resets drafts + search budgets with a logged event.

No compiler needed: judges and held-out validators are stubs, the import pipeline is
monkeypatched, and profile identities come from registry pins and (absent) install
manifests.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from decompnet import toolchains
from decompnet.app.engine import RomNetwork

from .test_calibration import make_profile


def match_judge():
    def judge(profile, asm, data, header):
        return {"verdict": "match", "score": 1.0, "evidence": f"ok for {asm}"}
    return judge


# ------------------------------------------------------------------------------
# calibrate_and_commit: bounded, held-out-gated, never mutates anything itself
# ------------------------------------------------------------------------------

def test_inconclusive_calibration_never_commits():
    calls = []
    report = toolchains.calibrate_and_commit(
        [make_profile(flags=["-O0"])], [("asm", "data", "header")],
        lambda p, a, d, h: {"verdict": "inconclusive", "score": None, "evidence": "cannot tell"},
        held_out=[("ha", "hd", "hh")], held_out_judge=match_judge(), commit=calls.append)
    assert not report.committed and "inconclusive" in report.reason
    assert report.profile is None and report.report.status == "inconclusive"
    assert calls == []  # the commit hook was never invoked


def test_no_held_out_set_refuses_to_commit():
    """The default is safety: without an explicit held-out set nothing is committed."""
    calls = []
    report = toolchains.calibrate_and_commit(
        [make_profile()], [("asm", "data", "header")], match_judge(), commit=calls.append)
    assert not report.committed and "held-out" in report.reason
    assert calls == []


def test_no_commit_hook_refuses():
    report = toolchains.calibrate_and_commit(
        [make_profile()], [("asm", "data", "header")], match_judge(),
        held_out=[("ha", "hd", "hh")], held_out_judge=match_judge())
    assert not report.committed and "commit" in report.reason


def test_held_out_validation_passes_commits_once():
    calls = []
    candidates = [make_profile(flags=["-O0"]), make_profile(flags=["-O2"])]
    report = toolchains.calibrate_and_commit(
        candidates, [("asm", "data", "header")],
        lambda p, a, d, h: {"verdict": "match" if "-O2" in p.flags else "wrong",
                            "score": 1.0, "evidence": ""},
        held_out=[("ha", "hd", "hh"), ("hb", "hd", "hh")], held_out_judge=match_judge(),
        commit=calls.append)
    assert report.committed and calls == [candidates[1]]
    assert report.profile == candidates[1]
    assert report.identity == candidates[1].identity()
    assert [r.verdict for r in report.held_out] == ["match", "match"]
    assert report.regression == ("not-run", "no regression hook supplied")


def test_held_out_disagreement_refuses():
    calls = []
    report = toolchains.calibrate_and_commit(
        [make_profile()], [("asm", "data", "header")], match_judge(),
        held_out=[("ha", "hd", "hh"), ("hb", "hd", "hh")],
        held_out_judge=lambda p, a, d, h: {"verdict": "wrong" if a == "hb" else "match",
                                           "score": 0.0, "evidence": "bytes disagree"},
        commit=calls.append)
    assert not report.committed and "held-out" in report.reason
    assert [r.verdict for r in report.held_out] == ["match", "wrong"]
    assert calls == []


def test_held_out_inconclusive_refuses():
    calls = []
    report = toolchains.calibrate_and_commit(
        [make_profile()], [("asm", "data", "header")], match_judge(),
        held_out=[("ha", "hd", "hh")],
        held_out_judge=lambda p, a, d, h: {"verdict": "inconclusive", "score": None,
                                           "evidence": "cannot validate"},
        commit=calls.append)
    assert not report.committed and calls == []


def test_held_out_judge_errors_refuse_not_crash():
    calls = []
    def broken(profile, asm, data, header):
        raise RuntimeError("validator crashed")
    report = toolchains.calibrate_and_commit(
        [make_profile()], [("asm", "data", "header")], match_judge(),
        held_out=[("ha", "hd", "hh")], held_out_judge=broken, commit=calls.append)
    assert not report.committed
    assert report.held_out[0].verdict == "inconclusive"
    assert "validator crashed" in report.held_out[0].evidence
    assert calls == []


def test_regression_hook_gates_the_commit():
    """P4.1.7: affected accepted matches are re-checked before a profile change commits."""
    def regress(profile):
        return False, "func_2 no longer matches"
    calls = []
    report = toolchains.calibrate_and_commit(
        [make_profile()], [("asm", "data", "header")], match_judge(),
        held_out=[("ha", "hd", "hh")], held_out_judge=match_judge(),
        regression=regress, commit=calls.append)
    assert not report.committed and report.regression == ("failed", "func_2 no longer matches")
    assert calls == []
    # a passing regression check lets the commit through, and the record says so
    report = toolchains.calibrate_and_commit(
        [make_profile()], [("asm", "data", "header")], match_judge(),
        held_out=[("ha", "hd", "hh")], held_out_judge=match_judge(),
        regression=lambda p: (True, "all 3 matched functions still match"), commit=calls.append)
    assert report.committed and report.regression[0] == "ok"
    assert calls == [make_profile()]


def test_regression_hook_exceptions_refuse():
    calls = []
    report = toolchains.calibrate_and_commit(
        [make_profile()], [("asm", "data", "header")], match_judge(),
        held_out=[("ha", "hd", "hh")], held_out_judge=match_judge(),
        regression=lambda p: (_ for _ in ()).throw(RuntimeError("linker gone")), commit=calls.append)
    assert not report.committed and report.regression[0] == "failed"
    assert calls == []


def test_held_out_validation_is_bounded():
    calls = []
    def held_judge(profile, asm, data, header):
        calls.append(asm)
        return {"verdict": "match", "score": 1.0, "evidence": ""}
    report = toolchains.calibrate_and_commit(
        [make_profile()], [("asm", "data", "header")], match_judge(),
        held_out=[(f"h{i}", "d", "h") for i in range(5)], held_out_judge=held_judge,
        max_held_out=2, commit=lambda p: None)
    assert report.committed and len(calls) == 2 and report.held_out_cut == 3


def test_commit_hook_exceptions_propagate():
    def boom(profile):
        raise RuntimeError("commit failed halfway")
    with pytest.raises(RuntimeError, match="commit failed halfway"):
        toolchains.calibrate_and_commit(
            [make_profile()], [("asm", "data", "header")], match_judge(),
            held_out=[("ha", "hd", "hh")], held_out_judge=match_judge(), commit=boom)


def test_sample_judge_and_held_out_judge_are_independent():
    """A sample judge that likes the profile cannot outvote a held-out validator."""
    calls = []
    report = toolchains.calibrate_and_commit(
        [make_profile()], [("asm", "data", "header")], match_judge(),
        held_out=[("ha", "hd", "hh")],
        held_out_judge=lambda p, a, d, h: {"verdict": "wrong", "score": 0.0,
                                           "evidence": "held-out disagrees"},
        commit=calls.append)
    assert not report.committed and calls == []


def test_ambiguous_calibration_never_commits():
    calls = []
    report = toolchains.calibrate_and_commit(
        [make_profile(), make_profile(flags=["-O0"])], [("asm", "data", "header")], match_judge(),
        held_out=[("ha", "hd", "hh")], held_out_judge=match_judge(), commit=calls.append)
    assert not report.committed and "ambiguous" in report.reason
    assert calls == []


# ------------------------------------------------------------------------------
# Engine wiring: provenance, events and the invalidation contract
# ------------------------------------------------------------------------------

def rom_net(tmp_path, compiler: dict, seed: str | None = "/* old seed */") -> RomNetwork:
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'fixture', 'fixture', '', 'mips', 0, '/tmp/fixture', ?)",
                   (json.dumps({"kind": "rom", "compiler": compiler}),))
        db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, seed, "
                   "unit, vram, rom_offset, status) VALUES(1, 1, 'func_1', 40, 5, NULL, '[]', '', ?, '', "
                   "0x80000000, 0, 'open')", (seed,))
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, 'int x;', NULL, 'fixture', 0, '')")
    return net


GUESS = {"toolchain": "gcc-2.7.2-kmc", "flags": ["-O2"], "timeout": 20, "provenance": "guess"}


def test_set_compiler_records_explicit_provenance_and_an_invalidation_event(tmp_path):
    net = rom_net(tmp_path, GUESS)
    net.set_compiler("fixture", "gcc-2.8.1-psx", ["-O0"])
    proj = net.store.one("SELECT config FROM projects WHERE id=1")
    compiler = json.loads(proj["config"])["compiler"]
    assert compiler["provenance"] == "explicit"
    assert compiler["toolchain"] == "gcc-2.8.1-psx" and compiler["flags"] == ["-O0"]
    event = net.store.one("SELECT * FROM events WHERE kind='compiler' ORDER BY id DESC LIMIT 1")
    assert event is not None
    assert "provenance guess -> explicit" in event["detail"]
    assert "invalidated" in event["detail"] and "search_budgets" in event["detail"]


def test_set_compiler_resets_drafts_and_budgets_only_on_identity_change(tmp_path):
    net = rom_net(tmp_path, GUESS)
    with net.store.tx() as db:
        db.execute("INSERT INTO search_budgets(function_id, turns_used, max_turns, last_at, cooldown_until) "
                   "VALUES(1, 3, 4, 10, 5)")
    net.set_compiler("fixture", "gcc-2.7.2-kmc", ["-O2"])  # same profile: provenance only
    budget = net.store.one("SELECT * FROM search_budgets WHERE function_id=1")
    assert budget["turns_used"] == 3  # untouched: nothing compiled differently
    assert net.store.one("SELECT seed FROM functions WHERE id=1")["seed"] == "/* old seed */"
    net.set_compiler("fixture", "gcc-2.8.1-psx", ["-O0"])  # a real profile change
    budget = net.store.one("SELECT * FROM search_budgets WHERE function_id=1")
    assert budget["turns_used"] == 0 and budget["cooldown_until"] == 0
    assert budget["reset_reason"] == "compiler-changed" and budget["epoch"].startswith("profile-")
    assert net.store.one("SELECT seed FROM functions WHERE id=1")["seed"] is None
    events = net.store.all("SELECT * FROM events WHERE kind='compiler' ORDER BY id")
    assert len(events) == 2
    # the provenance-only change is logged with no invalidation; the identity change logs the contract
    assert "guess -> explicit" in events[0]["detail"] and "invalidated" not in events[0]["detail"]
    assert "explicit -> explicit" in events[1]["detail"] and "invalidated" in events[1]["detail"]


def test_import_rom_stores_the_guess_profile(tmp_path, monkeypatch):
    from decompnet.app import engine
    from decompnet.rom import split

    info = {"title": "TEST GAME", "game_code": "NTST", "region": "Japan", "size": 0x1000,
            "sha1": "00" * 20, "compiler_guess": "KMC"}
    fn = split.RomFunction("func_1", 0x80000000, 0, "main",
                           [split.AsmLine(0, 0x80000000, 0, "nop", "")], Path(), set())

    class Picked:
        def __init__(self):
            self.data, self.info, self.platform, self.big_endian = bytes(0x1000), info, "n64", True
            self.overlays, self.filename = None, "test.z64"

    class Res:
        image, compiler, gp, named, data = None, "KMC", 0, {"func_1": 0x80000000}, []
        segments = [SimpleNamespace(name="main", rom_start=0, rom_end=0x1000, vram=0x80000000)]
        functions = [fn]

    monkeypatch.setattr(engine.detect, "identify", lambda data, name: Picked())
    monkeypatch.setattr(engine.split, "split_rom", lambda workdir, filename, log=None, overlays=None: Res())

    net = RomNetwork(tmp_path / "imp.db", tmp_path)
    slug = net.import_rom(bytes(0x1000), confirmed=True, log=lambda *_: None)
    assert slug == "test-game"
    compiler = json.loads(net.store.one("SELECT config FROM projects WHERE id=1")["config"])["compiler"]
    assert compiler == {"toolchain": "gcc-2.7.2-kmc", "flags": ["-G0", "-mips3", "-mgp32", "-mfp32", "-O2"],
                        "timeout": 20, "wrapper": "cc", "language": "c", "processor_mode": "",
                        "abi": [], "scope": "", "provenance": "guess"}


def test_import_gba_stores_the_guess_profile_with_its_cmd_wrapper(tmp_path, monkeypatch):
    from decompnet.app import engine
    from decompnet.rom import gba

    info = {"title": "TEST GBA", "game_code": "ATST", "region": "Japan", "size": 0x1000}

    class Picked:
        def __init__(self):
            self.info, self.filename = info, "test.gba"

    monkeypatch.setattr(gba, "split", lambda rom, work, log=None: [
        SimpleNamespace(name="func_1", code=b"\x00" * 4, n_insns=1, asm="", rom=0,
                        vram=gba.BASE, asm_only=None, refs=[])])

    net = RomNetwork(tmp_path / "gba.db", tmp_path)
    work = tmp_path / "gba-game"
    work.mkdir()
    slug = net._import_gba(Picked(), "gba-game", work, None, None, log=lambda *_: None)
    assert slug == "gba-game"
    compiler = json.loads(net.store.one("SELECT config FROM projects WHERE id=1")["config"])["compiler"]
    assert compiler["toolchain"] == "agbcc" and compiler["provenance"] == "guess"
    assert compiler["wrapper"] == "cmd"  # agbcc compiles through its full command template
