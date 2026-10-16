"""Stopping an agent with everything it started, UTF-8 output, the guides, the build's ninja and dtk's build paths."""

import os
import shutil
import subprocess
import sys
import time

import pytest

from decompnet.app import agents
from decompnet.forks.project import ForkProject


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_stopping_an_agent_stops_what_it_started(tmp_path):
    pidfile = tmp_path / "child.pid"
    code = ("import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
            "time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        time.sleep(0.1)
    child = int(pidfile.read_text())
    agents._terminate(proc)
    proc.wait(10)
    for _ in range(50):
        if not _alive(child):
            break
        time.sleep(0.1)
    assert not _alive(child)


def test_output_into_a_pipe_is_utf8():
    res = subprocess.run([sys.executable, "-I", "-c", "import decompnet; print('→ ✓ 🎉')"],
                         capture_output=True)
    assert res.returncode == 0 and res.stdout.decode("utf-8").strip() == "→ ✓ 🎉"


def test_the_guides_reach_agents_intact():
    from decompnet.app import tools

    text = tools._guide("fork")
    assert "…" in text and "â€" not in text


def test_builds_find_decomp_wtfs_own_ninja(tmp_path, monkeypatch):
    """ninja comes in decomp.wtf's own environment, which isn't on PATH once decomp.wtf is installed (a GameCube game's
    first build ran `ninja` and it wasn't found)."""
    own = os.path.normcase(os.path.dirname(sys.executable))
    monkeypatch.setenv("PATH", os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                                               if os.path.normcase(p.rstrip("\\/")) != own))
    if shutil.which("ninja"):
        pytest.skip("another ninja is on PATH")
    ok, out = ForkProject(tmp_path).run(["ninja", "--version"], log=lambda *_: None)
    assert ok, out


@pytest.mark.parametrize("target", ["build/G1E751/ok", r"build\G1E751\ok"])
def test_the_checksum_target_is_found_with_either_slash(tmp_path, target):
    (tmp_path / "build.ninja").write_text(f"build {target}: check config/G1E751/build.sha1\n", encoding="utf-8")
    assert ForkProject(tmp_path).check_target() == target


def test_the_guide_tool_lists_every_guide_topic():
    """The topic list in the guide tool's description comes from GUIDES (it used to name 9 of 22)."""
    import asyncio

    from decompnet.app import tools

    mcp = tools.build_mcp(None, lambda ctx: None)
    desc = next(t.description for t in asyncio.run(mcp.list_tools()) if t.name == "guide")
    assert all(topic in desc for topic in tools.GUIDES)


@pytest.mark.parametrize("platform", ["n64", "psx", "ps2", "gba", "gb", "gbc"])
def test_every_rom_platform_names_a_compiler_guide_that_exists(platform):
    from decompnet.app import tools
    from decompnet.rom import detect

    assert platform in detect.PLATFORMS
    toolchain = {"n64": "gcc-2.7.2-kmc"}.get(platform, "gbdk-2020")
    guide = agents.compiler_guide({"platform": platform, "compiler": {"toolchain": toolchain}})
    assert guide in tools.GUIDES
    assert guide == {"n64": "gcc-kmc", "psx": "psx", "ps2": "ps2", "gba": "agbcc", "gb": "gb", "gbc": "gb"}[platform]
    assert agents.compiler_guide({"platform": "n64", "compiler": {"toolchain": "ido-7.1"}}) == "ido"


def test_a_game_boy_helper_is_pointed_at_the_game_boy_guide(monkeypatch):
    """The helper prompt named "gcc-kmc" (the N64 compiler) as a Game Boy project's compiler guide."""
    from decompnet.app.agents import Manager, Session

    ov = {"kind": "rom", "title": "Some Game", "platform": "gb", "compiler": {"toolchain": "gbdk-2020"}}
    manager = Manager.__new__(Manager)
    manager.net = type("Net", (), {"overview": staticmethod(lambda slug: ov)})()
    text = manager._prompt(Session("claude-1", "claude", "Claude Code #1", "game", "claude-1", {}))
    assert "gcc-kmc" not in text and "gb guides" in text
