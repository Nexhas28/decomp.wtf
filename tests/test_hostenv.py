"""decomp.wtf on the user's own Mac: paths the user gives, and compiles started from anywhere."""

import pytest

from decompnet import hostenv, toolchains


def test_native_changes_nothing():
    assert hostenv.info() == {"runtime": "native", "host_os": "macos"}
    assert str(hostenv.local_path("~/x")).endswith("/x") and "~" not in str(hostenv.local_path("~/x"))
    assert str(hostenv.local_path('"/Users/sam/roms/game.z64"')) == "/Users/sam/roms/game.z64"  # quoted
    assert hostenv.shown_path("/Users/sam/Decomp") == "/Users/sam/Decomp"


@pytest.mark.skipif(toolchains.installed("gcc-2.7.2-kmc") is None, reason="needs gcc-2.7.2-kmc")
def test_compiles_when_the_app_runs_from_root(monkeypatch):
    """A service starts decomp.wtf in /: KMC GCC's assembler can't read its input from there, so every compile runs
    in its own work folder."""
    from decompnet import compiler, project

    monkeypatch.chdir("/")
    cfg = project.compiler_config(hostenv.Path("/tmp"), {"compiler": {"toolchain": "gcc-2.7.2-kmc", "sandbox": "none"}})
    res = compiler.compile_source(cfg, "", "int f(int v) { return v < 0 ? -v : v; }", trusted=True)
    assert res.ok, res.log
