"""The Evidence section of compile_and_diff: the tool output a diff calls for, handed over instead of hinted at.

- a structure diff (instructions missing/extra) gets two readings: the m2c draft's skeleton and rz-ghidra's draft
  (through the local tools, mocked here), on the agent's first compiles only
- an operand diff ('|' / 'i') gets the nearest matched look-alike's source (find_similar's hit, mocked)
- a register/stack/order-only diff gets the one triage line and no tool output
- the tools run once per (function, class): a repeated compile is served from the cache
- the section stays under EVIDENCE_MAX_CHARS whatever the tools wrote
- degrades: no rz-ghidra -> m2c only, with a line saying so; no look-alike -> one line saying so
- the claim brief opens the best attempt with the same triage line
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from decompnet import network as netmod
from decompnet.app import localtools
from decompnet.network import (EVIDENCE_DRAFT_LINES, EVIDENCE_MAX_CHARS, Network, NetworkError, c_structure_summary,
                               diff_class, triage_line)
from decompnet.scoring import DiffResult

M2C = """s32 func_80001000(s32 arg0) {
    s32 i;
    if (arg0 == 0) {
        return -1;
    }
    for (i = 0; i < 4; i++) {
        helper(i);
    }
    return 0;
}
"""

STRUCTURE_ROWS = [(" ", "addiu $sp, $sp, -0x18", "addiu $sp, $sp, -0x18"), ("<", "beqz $a0, .L1", ""),
                  ("<", "nop", ""), (" ", "jal helper", "jal helper"), (">", "", "addiu $a0, $a0, 1"),
                  (" ", "jr $ra", "jr $ra")]
OPERAND_ROWS = [(" ", "lw $t0, 0($a0)", "lw $t0, 0($a0)"), ("i", "lw $t1, 8($a0)", "lw $t1, 4($a0)"),
                ("|", "addiu $v0, $t1, 2", "sll $v0, $t1, 2"), (" ", "jr $ra", "jr $ra")]
REGISTER_ROWS = [(" ", "lw $t0, 0($a0)", "lw $t0, 0($a0)"), ("r", "addu $v0, $t0, $a1", "addu $v0, $t1, $a1"),
                 ("s", "sw $v0, 0x10($sp)", "sw $v0, 0x14($sp)"), ("o", "nop", "nop"), (" ", "jr $ra", "jr $ra")]
ENCODING_ROWS = [(" ", "jr $ra", "jr $ra"), (" ", "nop", "nop")]


class FakeResult:
    ok, log, obj = True, "", b"\x03\xe0\x00\x08"


class FakeCand:
    @staticmethod
    def fingerprint():
        return "cand"


class LocalNet(Network):
    """A coordinator with the two things the local tools ask of the app's (its folder and config)."""

    def __init__(self, db, workspace):
        super().__init__(db)
        self.workspace = workspace

    def project_row(self, slug):
        return self._project(slug)

    @staticmethod
    def cfg(proj):
        return json.loads(proj["config"])


@pytest.fixture
def net(tmp_path):
    n = LocalNet(tmp_path / "net.db", tmp_path)
    with n.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'game', 'game', 'evidence fixture', 'mips', 1, ?, ?)",
                   (str(tmp_path), json.dumps({"kind": "rom", "rom": {"file": "rom.z64"}})))
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, 'void helper(int);', NULL, 'fixture', 0, '')")
        db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, seed, status, "
                   "vram, rom_offset) VALUES(1, 1, 'func_80001000', 24, 6, ?, '[]', 'func_80001000:', ?, 'open', "
                   "?, ?)", (b"\x00" * 24, M2C, 0x80001000, 0x1000))
        db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, status, "
                   "matched_source, vram) VALUES(2, 1, 'func_80002000', 16, 4, ?, '[]', 'func_80002000:', 'matched', "
                   "?, ?)", (b"\x01" * 16, "s32 func_80002000(Obj *o) { return o->field + 2; }", 0x80002000))
    return n


@pytest.fixture
def alice(net):
    return net.authenticate(net.register_agent("alice"))


def judge_with(net, monkeypatch, rows, score=40):
    """compile_and_diff with the compiler, evaluator and scorer stubbed to produce exactly these diff rows."""
    monkeypatch.setattr(net, "_compile", lambda proj, header, source, fn=None: FakeResult())
    monkeypatch.setattr(net, "_evaluate", lambda fn, proj, obj: (FakeCand(), None, []))
    monkeypatch.setattr(net, "_target_obj", lambda fn, proj: None)
    monkeypatch.setattr(net, "_worth_variant", lambda verdict, d: False)
    monkeypatch.setattr(net, "_validity", lambda fn, source: None)
    monkeypatch.setattr(netmod.scoring, "diff", lambda target, cand, **kw: DiffResult(score, 600, list(rows), False))


class Tools:
    """The local tools as the evidence code sees them: rz-ghidra ready (or not), its run counted."""

    def __init__(self, monkeypatch, *, installed=True, output=None):
        self.runs: list[dict] = []
        self.output = output if output is not None else (
            "rz-ghidra decompile: ok\nWARNING: no symbols\n"
            "undefined4 fcn_80001000(int param_1)\n{\n    int i;\n    if (param_1 == 0) {\n        return 0xffffffff;\n"
            "    }\n    for (i = 0; i < 4; i = i + 1) {\n        fcn_80000800(i);\n    }\n    return 0;\n}\n"
            "Files written (pass them as `file` to another tool):\n  tool-output/x (12 bytes)")

        def tool(name):
            if installed and name == "rz-ghidra":
                return {"name": name}
            raise NetworkError(f"no tool {name!r} here; local_tools() lists them")

        def run(net, slug, tool, operation, agent="you", **args):
            self.runs.append({"slug": slug, "tool": tool, "operation": operation, "agent": agent, **args})
            return self.output

        monkeypatch.setattr(localtools, "_tool", tool)
        monkeypatch.setattr(localtools, "status", lambda t: ("ready", ""))
        monkeypatch.setattr(localtools, "run", run)


def similar_hits(net, monkeypatch, hits):
    """find_similar's ranking, as the app's coordinator provides it: [(function row, similarity)]."""
    calls = []

    def similar(proj, fn, top=5):
        calls.append(fn["name"])
        return [(net.store.one("SELECT * FROM functions WHERE id=?", fid), sim) for fid, sim in hits]

    monkeypatch.setattr(net, "similar", similar, raising=False)
    return calls


# ------------------------------------------------------------------------------------- classification

def test_diff_class_and_triage_cover_every_marker():
    assert diff_class(STRUCTURE_ROWS) == "structure"
    assert diff_class(OPERAND_ROWS) == "operand"
    assert diff_class(REGISTER_ROWS) == "register"
    assert diff_class(ENCODING_ROWS) == "none"
    assert diff_class([("<", "nop", ""), ("r", "a", "b")]) == "structure"  # the worst kind wins
    assert diff_class([("i", "a", "b"), ("s", "a", "b")]) == "operand"
    t = triage_line(STRUCTURE_ROWS)
    assert t.startswith("triage: STRUCTURE") and "target 5 insns, yours 4" in t and "2 missing, 1 extra" in t
    assert "2 operand/instruction diffs" in triage_line(OPERAND_ROWS)
    assert triage_line(REGISTER_ROWS) == ("triage: count matches, 3 register diffs: statement order/temporaries; "
                                          "don't change the logic")
    assert "only encodings differ" in triage_line(ENCODING_ROWS)


def test_structure_summary_keeps_the_control_flow_and_bounds_it():
    s = c_structure_summary(M2C)
    assert s.splitlines()[0] == "1 if, 1 for, 2 return; calls: func_80001000, helper"
    assert "if (arg0 == 0) {" in s and "for (i = 0; i < 4; i++) {" in s and "helper(i);" in s
    assert "s32 i;" not in s  # a declaration shapes nothing
    big = "void f(void) {\n" + "    if (x) g();\n" * 80 + "}\n"
    lines = c_structure_summary(big, max_lines=10).splitlines()
    assert len(lines) == 12 and lines[-1] == "… 71 more lines"


# ------------------------------------------------------------------------------- evidence per class

def test_a_structure_diff_hands_over_two_drafts(net, alice, monkeypatch):
    tools = Tools(monkeypatch)
    judge_with(net, monkeypatch, STRUCTURE_ROWS)
    out = net.compile_and_diff(alice, "func_80001000", "int func_80001000(int a) { helper(a + 1); return 0; }")
    assert "### Evidence" in out and "triage: STRUCTURE" in out
    assert "m2c draft, structure only:" in out and "for (i = 0; i < 4; i++) {" in out
    assert "rz-ghidra draft" in out and "for (i = 0; i < 4; i = i + 1) {" in out
    assert "WARNING" not in out and "Files written" not in out  # the run's noise is dropped
    assert tools.runs == [{"slug": "game", "tool": "rz-ghidra", "operation": "decompile", "agent": "evidence",
                           "file": "game", "arch": "mips-be", "address": "0x80001000", "base": "0x80000000"}]


def test_an_operand_diff_hands_over_the_lookalikes_source(net, alice, monkeypatch):
    tools = Tools(monkeypatch)
    calls = similar_hits(net, monkeypatch, [(2, 0.91)])
    judge_with(net, monkeypatch, OPERAND_ROWS)
    out = net.compile_and_diff(alice, "func_80001000", "int func_80001000(Obj *o) { return o->x << 2; }")
    assert "### Evidence" in out and "triage: OPERANDS" in out
    assert "nearest matched look-alike: func_80002000 (91% similar, 4 insns)" in out
    assert "the same operand shape appears here" in out
    assert "return o->field + 2;" in out  # its source, to compare symbols and offsets against
    assert calls == ["func_80001000"] and tools.runs == []  # no decompiler for an operand diff


def test_a_register_only_diff_gets_the_triage_line_and_nothing_else(net, alice, monkeypatch):
    tools = Tools(monkeypatch)
    calls = similar_hits(net, monkeypatch, [(2, 0.91)])
    judge_with(net, monkeypatch, REGISTER_ROWS)
    out = net.compile_and_diff(alice, "func_80001000", "int func_80001000(int *p, int b) { return *p + b; }")
    evidence = out.split("### Evidence\n", 1)[1]
    assert evidence.splitlines()[0] == ("triage: count matches, 3 register diffs: statement order/temporaries; "
                                        "don't change the logic")
    assert "look-alike" not in evidence and "draft" not in evidence
    assert tools.runs == [] and calls == []


def test_an_encoding_only_diff_gets_no_evidence_section(net, alice, monkeypatch):
    Tools(monkeypatch)
    judge_with(net, monkeypatch, ENCODING_ROWS, score=3)
    out = net.compile_and_diff(alice, "func_80001000", "void func_80001000(void) {}")
    assert "NOT A MATCH YET" in out and "### Evidence" not in out


# ------------------------------------------------------------------------------- bounds and caching

def test_tools_run_once_per_function_and_class(net, alice, monkeypatch):
    tools = Tools(monkeypatch)
    judge_with(net, monkeypatch, STRUCTURE_ROWS)
    first = net.compile_and_diff(alice, "func_80001000", "int func_80001000(int a) { return 1; }")
    second = net.compile_and_diff(alice, "func_80001000", "int func_80001000(int a) { return 2; }")
    assert len(tools.runs) == 1
    assert first.split("### Evidence")[1] == second.split("### Evidence")[1]
    # the third compile with the same structure diff: the drafts were shown already, the verdict stays
    third = net.compile_and_diff(alice, "func_80001000", "int func_80001000(int a) { return 3; }")
    assert "triage: STRUCTURE" in third and "rz-ghidra draft" not in third and len(tools.runs) == 1
    # another agent's first compile sees them again, from the cache
    bob = net.authenticate(net.register_agent("bob"))
    net.release(alice, "func_80001000", "handing over")
    assert "rz-ghidra draft" in net.compile_and_diff(bob, "func_80001000", "int func_80001000(int a) { return 4; }")
    assert len(tools.runs) == 1


def test_the_section_stays_bounded_whatever_the_tools_write(net, alice, monkeypatch):
    draft = "rz-ghidra decompile: ok\n" + "".join(f"    x{i} = *(int *)(param_1 + {i * 4});\n" for i in range(400))
    Tools(monkeypatch, output=draft)
    with net.store.tx() as db:
        db.execute("UPDATE functions SET seed=? WHERE id=1", ("void f(void) {\n" + "    if (x) g();\n" * 200 + "}\n",))
    judge_with(net, monkeypatch, STRUCTURE_ROWS)
    out = net.compile_and_diff(alice, "func_80001000", "int func_80001000(int a) { return 1; }")
    evidence = "### Evidence" + out.split("### Evidence", 1)[1].split("\n\n⏭", 1)[0]
    assert len(evidence) <= EVIDENCE_MAX_CHARS + 60
    assert "triage: STRUCTURE" in evidence and "m2c draft" in evidence
    assert f"{EVIDENCE_DRAFT_LINES} lines" in evidence


def test_a_long_lookalike_source_is_cut(net, alice, monkeypatch):
    with net.store.tx() as db:
        db.execute("UPDATE functions SET matched_source=? WHERE id=2", ("int g(void) {\n" + "    y += 1;\n" * 300 + "}",))
    similar_hits(net, monkeypatch, [(2, 0.7)])
    judge_with(net, monkeypatch, OPERAND_ROWS)
    out = net.compile_and_diff(alice, "func_80001000", "int func_80001000(void) { return 1; }")
    evidence = "### Evidence" + out.split("### Evidence", 1)[1]
    assert "… (cut)" in evidence and len(evidence) <= EVIDENCE_MAX_CHARS


# ---------------------------------------------------------------------------------- degrading

def test_without_rz_ghidra_the_m2c_draft_stands_alone(net, alice, monkeypatch):
    tools = Tools(monkeypatch, installed=False)
    judge_with(net, monkeypatch, STRUCTURE_ROWS)
    out = net.compile_and_diff(alice, "func_80001000", "int func_80001000(int a) { return 1; }")
    assert "m2c draft, structure only:" in out
    assert "no second draft: rz-ghidra isn't installed here" in out and tools.runs == []


def test_a_tool_that_fails_never_fails_the_result(net, alice, monkeypatch):
    Tools(monkeypatch)
    monkeypatch.setattr(localtools, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    judge_with(net, monkeypatch, STRUCTURE_ROWS)
    out = net.compile_and_diff(alice, "func_80001000", "int func_80001000(int a) { return 1; }")
    assert "score 40" in out and "evidence unavailable: RuntimeError: boom" in out


def test_without_a_lookalike_one_line_says_so(net, alice, monkeypatch):
    similar_hits(net, monkeypatch, [])
    judge_with(net, monkeypatch, OPERAND_ROWS)
    out = net.compile_and_diff(alice, "func_80001000", "int func_80001000(void) { return 1; }")
    assert "no matched look-alike yet" in out and "```c" not in out.split("### Evidence")[1]


def test_a_plain_coordinator_without_tools_or_similarity_still_answers(tmp_path, monkeypatch):
    """The base Network (no project_row/workspace, no similar): both lookups degrade to a line each."""
    n = Network(tmp_path / "plain.db")
    with n.store.tx() as db:
        db.execute("INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
                   "VALUES(1, 'p', 'p', '', 'mips', 1, ?, '{}')", (str(tmp_path),))
        db.execute("INSERT INTO context_versions(project_id, version, header, agent_id, message, created_at, unit) "
                   "VALUES(1, 1, '', NULL, 'm', 0, '')")
        db.execute("INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, status, vram) "
                   "VALUES(1, 1, 'f', 8, 2, ?, '[]', 'f:', 'open', 1)", (b"\x00" * 8,))
    a = n.authenticate(n.register_agent("a"))
    judge_with(n, monkeypatch, STRUCTURE_ROWS)
    out = n.compile_and_diff(a, "f", "int f(void) { return 1; }")
    assert "no m2c draft for this function" in out and "no second draft: this coordinator has no local tools" in out
    judge_with(n, monkeypatch, OPERAND_ROWS)
    assert "no matched look-alike yet" in n.compile_and_diff(a, "f", "int f(void) { return 2; }")


# ------------------------------------------------------------------------------------ the brief

def test_the_claim_brief_opens_the_best_attempt_with_its_triage(net, alice, monkeypatch):
    Tools(monkeypatch)
    judge_with(net, monkeypatch, STRUCTURE_ROWS, score=90)
    net.compile_and_diff(alice, "func_80001000", "int func_80001000(int a) { return 1; }")
    judge_with(net, monkeypatch, REGISTER_ROWS, score=20)  # better: the brief's verdict follows the best
    net.compile_and_diff(alice, "func_80001000", "int func_80001000(int a) { return 2; }")
    judge_with(net, monkeypatch, STRUCTURE_ROWS, score=90)  # worse: the verdict stays with the best
    net.compile_and_diff(alice, "func_80001000", "int func_80001000(int a) { return 3; }")
    net.release(alice, "func_80001000", "notes")
    bob = net.authenticate(net.register_agent("bob"))
    brief = net.claim(bob, "game", "func_80001000")
    best = brief.split("### Best attempt so far", 1)[1]
    assert best.splitlines()[0].startswith(": score 20 by alice")
    assert best.splitlines()[1] == ("triage: count matches, 3 register diffs: statement order/temporaries; "
                                    "don't change the logic")
    assert "return 2;" in best
