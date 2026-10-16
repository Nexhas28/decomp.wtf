"""Tests for the undefined-behaviour guard.

Offline tests use a stub compiler; the end-to-end test re-uses the archived
perf-test artefact that motivated the guard and skips when it (or the project DB)
is not present on this machine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from decompnet import validity


@dataclass
class _Cfg:
    cmd: list[str] = field(default_factory=lambda: ["gcc", "-c", "{src}", "-o", "{obj}"])
    cache: bool = True


class _Result:
    def __init__(self, log: str):
        self.log = log
        self.ok = True


def _stub(log: str, seen: list | None = None):
    def compile_source(cfg, header, source):
        if seen is not None:
            seen.append(cfg)
        return _Result(log)

    return compile_source


# --------------------------------------------------------------------------- parsing


def test_warnings_in_finds_gcc_wording():
    log = ("func.c:12: warning: `new_var' might be used uninitialized in this function\n"
           "func.c:20: warning: unused variable `x'\n")
    hits = validity.warnings_in(log)
    assert len(hits) == 1 and "new_var" in hits[0]


def test_warnings_in_handles_empty_log():
    assert validity.warnings_in("") == ()
    assert validity.warnings_in(None) == ()  # type: ignore[arg-type]


def test_warnings_in_matches_modern_wording():
    assert validity.warnings_in("w.c:3:13: warning: 'p' is used uninitialized [-Wuninitialized]")


# --------------------------------------------------------------------------- check


def test_check_ok_without_warnings():
    verdict = validity.check(_Cfg(), "header", "int f(void){return 0;}", _stub("clean build\n"))
    assert verdict.ok and verdict.warnings == ()
    assert verdict.as_dict() == {"ok": True, "warnings": [], "detail": ""}


def test_check_reports_uninitialised_use():
    log = "func.c:9: warning: `p' may be used uninitialized in this function\n"
    verdict = validity.check(_Cfg(), "header", "int f(void){return *(int *)p;}", _stub(log))
    assert verdict.ok is False
    assert "may be used uninitialized" in verdict.detail
    assert validity.describe(verdict).startswith("uninitialised use:")


def test_check_skips_without_a_compiler_config():
    verdict = validity.check(None, "header", "int f(void){return 0;}", _stub(""))
    assert verdict.ok and "skipped" in verdict.detail


def test_check_enables_the_warning_and_bypasses_the_cache():
    seen: list = []
    validity.check(_Cfg(), "header", "int f(void){return 0;}", _stub("", seen))
    cfg = seen[0]
    assert "-Wuninitialized" in cfg.cmd and "-Wall" in cfg.cmd
    assert cfg.cache is False, "the probe must not poison the content-addressed cache"


def test_check_does_not_duplicate_the_flag():
    seen: list = []
    validity.check(_Cfg(cmd=["gcc", "-Wuninitialized"]), "h", "int f(void){return 0;}", _stub("", seen))
    assert seen[0].cmd.count("-Wuninitialized") == 1


def test_describe_is_one_line():
    verdict = validity.Validity(ok=False, warnings=("a", "b", "c", "d"), detail="a")
    text = validity.describe(verdict)
    assert "\n" not in text and text.count("|") == 2  # only the first three are shown


# --------------------------------------------------------------------------- end to end


def test_guard_catches_the_archived_artefact():
    """The source that scored 10/100 must be rejected; the valid one must pass."""
    root = Path(__file__).resolve().parents[2]
    artifact = root / "work" / "perf-test" / "a4620_UB_artifact_score10.c"
    clean = root / "work" / "perf-test" / "a4620_valid_best.c"
    db = root / ".decomp" / "decomp.db"
    if not (artifact.exists() and clean.exists() and db.exists()):
        pytest.skip("perf-test artefacts or the project DB are not present")

    import sys

    sys.path.insert(0, str(root / "decomp-wtf" / "src"))
    from decompnet.app.engine import RomNetwork
    from decompnet.compiler import compile_source
    from decompnet.project import compiler_config

    net = RomNetwork(str(db), root)
    proj = net.project_row("slus-01363-4")
    cfg = json.loads(proj["config"])
    fn = net._function("func_800A4620", "slus-01363-4")
    ctx = net._context(fn["project_id"], fn["unit"])
    cc = compiler_config(Path(proj["root"]), cfg)

    bad = validity.check(cc, ctx["header"], artifact.read_text(), compile_source)
    good = validity.check(cc, ctx["header"], clean.read_text(), compile_source)
    assert bad.ok is False, "the uninitialised-pointer artefact must be flagged"
    assert "uninitialized" in bad.detail
    assert good.ok is True, "the semantically valid reconstruction must pass"
