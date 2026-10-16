"""The local reverse-engineering tools (local-tools/): listed for people, run for agents only through checked operations."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from decompnet.app import agents, localtools, tools
from decompnet.network import NetworkError

REAL = Path(os.environ.get("DECOMP_TEST_LOCAL_TOOLS", Path(__file__).resolve().parents[1] / "local-tools"))
built = pytest.mark.skipif(not (REAL / "rizin" / "bin" / "rizin").exists(), reason="the local tools aren't built here")


class Net:
    """The two things localtools asks of the network: a project's folder and its config."""

    def __init__(self, root: Path, rom: str | None = "rom.z64"):
        self.root, self.rom = root, rom
        self.workspace = root.parent / "home"

    def project_row(self, slug):
        return {"root": str(self.root), "config": json.dumps({"rom": {"file": self.rom}} if self.rom else {})}

    @staticmethod
    def cfg(proj):
        return json.loads(proj["config"])


def fake_tools(tmp_path: Path, tools_: dict[str, str]) -> Path:
    """A local-tools folder whose tools are tiny shell scripts (name -> script body)."""
    root = tmp_path / "local-tools"
    entries = []
    for name, script in tools_.items():
        d = root / name / "bin"
        d.mkdir(parents=True)
        exe = d / name
        exe.write_text("#!/bin/sh\n" + script + "\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        for rel in localtools.NEEDS.get(name, []):  # the other programs its operations start
            extra = root / name / rel
            extra.parent.mkdir(parents=True, exist_ok=True)
            extra.touch()
        entries.append({"name": name, "category": "test", "entry": f"bin/{name}", "status": "pass", "folder": name})
    (root / "manifest.json").write_text(json.dumps({"tools": entries}))
    return root


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "rom.z64").write_bytes(bytes(range(256)) * 4)
    (tmp_path / "secret.txt").write_text("not yours")
    return proj


def test_agents_get_the_two_tools_everywhere():
    for fork in (False, True):
        assert {"local_tools", "run_tool"} <= set(tools.tool_names(fork))
    names = {d["function"]["name"] if "function" in d else d["name"] for d in agents.api_tool_schemas()}
    assert {"local_tools", "run_tool"} <= names
    assert "run_tool" in tools.WORK_PROMPT and "local_tools()" in tools.INSTRUCTIONS


def test_files_outside_the_project_are_refused(tmp_path, project, monkeypatch):
    monkeypatch.setenv("DECOMP_LOCAL_TOOLS", str(fake_tools(tmp_path, {"binwalk": 'echo "scanned $1"'})))
    net = Net(project)
    for bad in ("../secret.txt", str(tmp_path / "secret.txt"), "/etc/hosts", "tool-output/../../secret.txt"):
        with pytest.raises(NetworkError, match="outside the project"):
            localtools.run(net, "p", "binwalk", "scan", file=bad)
    out = localtools.run(net, "p", "binwalk", "scan", file="game")
    assert "binwalk scan: ok" in out and "scanned" in out and "rom.z64" in out


def test_unknown_tools_operations_and_values_are_refused(tmp_path, project, monkeypatch):
    monkeypatch.setenv("DECOMP_LOCAL_TOOLS", str(fake_tools(tmp_path, {"crunch64": "echo $@", "armips": "echo hi"})))
    net = Net(project)
    with pytest.raises(NetworkError, match="no tool"):
        localtools.run(net, "p", "nope", "x")
    with pytest.raises(NetworkError, match="can: decompress, compress"):
        localtools.run(net, "p", "crunch64", "rm")
    with pytest.raises(NetworkError, match="format must be one of"):
        localtools.run(net, "p", "crunch64", "decompress", file="game", format="yay0; rm -rf ~")
    with pytest.raises(NetworkError, match="no operations for agents"):
        localtools.run(net, "p", "armips", "assemble")


def test_a_slice_of_the_game_goes_in_and_results_can_be_reused(tmp_path, project, monkeypatch):
    monkeypatch.setenv("DECOMP_LOCAL_TOOLS", str(fake_tools(tmp_path, {"crunch64": 'wc -c < "$3"; cp "$3" "$4"'})))
    net = Net(project)
    out = localtools.run(net, "p", "crunch64", "decompress", file="game", format="yay0", offset="0x10", length="32")
    assert out.splitlines()[1].strip() == "32"
    assert "tool-output/crunch64-decompress/out.bin" in out
    again = localtools.run(net, "p", "crunch64", "compress", file="tool-output/crunch64-decompress/out.bin", format="yaz0")
    assert "32" in again
    # results stay out of the project folder (a fork's is committed to git), one folder per agent
    assert not (project / "tool-output").exists()
    other = localtools.run(net, "p", "crunch64", "decompress", agent="mewtwo", file="game", format="yay0", length="8")
    assert "8" in other.splitlines()[1]
    mine = localtools.results_folder(net, "p", "you") / "crunch64-decompress" / "out.bin"
    assert mine.stat().st_size == 32
    with pytest.raises(NetworkError, match="no file"):
        localtools.run(net, "p", "crunch64", "compress", agent="pikachu", file="tool-output/crunch64-decompress/out.bin",
                       format="yaz0")


def test_a_tool_that_hangs_is_stopped(tmp_path, project, monkeypatch):
    monkeypatch.setenv("DECOMP_LOCAL_TOOLS", str(fake_tools(tmp_path, {"ndstool": "sleep 30"})))
    monkeypatch.setattr(localtools.OPS["ndstool"]["info"], "timeout", 1)
    out = localtools.run(Net(project), "p", "ndstool", "info", file="game")
    assert "timed out after 1 s" in out


def test_missing_and_failing_tools_say_why(tmp_path, monkeypatch):
    root = fake_tools(tmp_path, {"rizin": "true"})
    m = json.loads((root / "manifest.json").read_text())
    m["tools"] += [{"name": "retdec", "category": "decompiler", "entry": "run", "status": "pass", "folder": "retdec"},
                   {"name": "findcode", "category": "rom-analysis", "entry": "bin/findcode", "status": "fail",
                    "folder": "findcode"}]
    (root / "manifest.json").write_text(json.dumps(m))
    monkeypatch.setenv("DECOMP_LOCAL_TOOLS", str(root))
    states = {t["name"]: t["state"] for t in localtools.summary()}
    assert states == {"rizin": "ready", "retdec": "not built", "findcode": "failing"}
    listing = localtools.describe()
    assert "- rizin: disassemble_bytes" in listing and "findcode" in listing


def test_a_wrapper_without_its_program_is_not_ready(tmp_path, monkeypatch):
    root = fake_tools(tmp_path, {"rizin": "true"})
    (root / "retdec").mkdir()
    (root / "retdec" / "run").write_text("#!/bin/sh\n")  # the wrapper is in git; the program it starts isn't built
    m = json.loads((root / "manifest.json").read_text())
    m["tools"].append({"name": "retdec", "category": "decompiler", "entry": "run", "status": "pass", "folder": "retdec"})
    (root / "manifest.json").write_text(json.dumps(m))
    monkeypatch.setenv("DECOMP_LOCAL_TOOLS", str(root))
    assert {t["name"]: t["state"] for t in localtools.summary()}["retdec"] == "not built"


def test_a_worktree_uses_the_main_checkouts_built_tools(tmp_path, monkeypatch):
    main = tmp_path / "main"
    built = fake_tools(main, {"rizin": "true"})
    wt = tmp_path / "wt"
    (wt / "local-tools").mkdir(parents=True)
    (wt / "local-tools" / "manifest.json").write_text((built / "manifest.json").read_text())  # docs only, nothing built
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n")
    monkeypatch.delenv("DECOMP_LOCAL_TOOLS", raising=False)
    monkeypatch.setattr(localtools, "__file__", str(wt / "src" / "decompnet" / "app" / "localtools.py"))
    assert localtools.folder() == built


def test_no_folder_no_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("DECOMP_LOCAL_TOOLS", str(tmp_path / "nowhere"))
    monkeypatch.setattr(localtools, "__file__", str(tmp_path / "a" / "b" / "c" / "d.py"))
    assert localtools.folder() is None and localtools.summary() == []
    assert "No local tools" in localtools.describe()


def test_every_operation_names_a_real_manifest_tool():
    names = {t["name"] for t in json.loads((Path(__file__).resolve().parents[1] / "local-tools" / "manifest.json")
                                           .read_text())["tools"]}
    assert set(localtools.OPS) <= names and set(localtools.PEOPLE_ONLY) <= names
    # every tool is either runnable by agents or says why it's for people (Ghidra extensions say so themselves)
    ghidra = {n for n in names if "ghidra" in n and n not in ("rz-ghidra", "ghidriff")}
    assert names == set(localtools.OPS) | set(localtools.PEOPLE_ONLY) | ghidra


# ------------------------------------------------------------------ the real tools, when they're built here

@built
@pytest.mark.skipif(sys.platform != "darwin", reason="the tools are built for macOS")
def test_real_tools_on_mips_code(tmp_path, monkeypatch):
    monkeypatch.setenv("DECOMP_LOCAL_TOOLS", str(REAL))
    proj = tmp_path / "proj"
    proj.mkdir()
    # addiu sp,sp,-0x18 ; jr ra ; nop  at 0x80000400 in a raw file loaded at 0x80000000
    (proj / "rom.z64").write_bytes(b"\0" * 0x400 + bytes.fromhex("27bdffe803e0000800000000"))
    net = Net(proj)
    out = localtools.run(net, "p", "rizin", "disassemble", file="game", arch="mips-be", address="0x80000400",
                         base="0x80000000", count=2)
    assert "addiu sp, sp, -0x18" in out and "jr" in out
    out = localtools.run(net, "p", "rizin", "disassemble_bytes", arch="mips-be", hex="27bdffe8")
    assert "addiu sp, sp, -0x18" in out
    out = localtools.run(net, "p", "keystone", "assemble", arch="mipsbe", text="addiu $sp, $sp, -24")
    assert "27 bd ff e8" in out
    out = localtools.run(net, "p", "pypcode", "pcode", arch="mips-be", hex="27bdffe8", address="0x80000400")
    assert "sp = sp + 0xffffffe8" in out
    out = localtools.run(net, "p", "rz-ghidra", "decompile", file="game", arch="mips-be", address="0x80000400",
                         base="0x80000000")
    assert "void" in out or "return" in out


def test_the_gui_lists_them_and_checks_one(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from decompnet.app.engine import RomNetwork
    from decompnet.app.server import build

    monkeypatch.setenv("DECOMP_LOCAL_TOOLS", str(fake_tools(tmp_path, {"rizin": "true"})))
    (localtools.folder() / "rizin" / "test.sh").write_text("echo checked; exit 0\n")
    home = tmp_path / "home"
    (home / ".decomp").mkdir(parents=True)
    mcp, _, security = build(RomNetwork(home / ".decomp" / "decomp.db", home), home / ".decomp", 6498, "k")
    client = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"),
                        base_url="http://127.0.0.1:6498")
    client.cookies.set("decomp_live", "k")
    listed = client.get("/api/state").json()["local_tools"]
    assert listed == [{"name": "rizin", "category": "test", "repo": "", "version": "", "state": "ready", "why": "",
                       "agents": ["disassemble", "disassemble_bytes", "strings"], "people": ""}]
    assert client.post("/api/localtools/test", json={"tool": "rizin"}).status_code == 403  # no X-Decomp header
    r = client.post("/api/localtools/test", json={"tool": "rizin"}, headers={"X-Decomp": "1"}).json()
    assert r == {"ok": True, "log": "checked"}
    assert client.post("/api/localtools/test", json={"tool": "nope"}, headers={"X-Decomp": "1"}).status_code == 400
