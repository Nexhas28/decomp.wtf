"""maspsx load-delay-nop drop spike (plan C.2): a bounded, opt-in post-assembler pass.

The PlayStation pipeline (gcc-2.7.2-psx -> maspsx -> GNU as 2.6) materialises a real ``nop`` between a
load and a following jump when maspsx decides the load's delay slot must be filled (its
``nop # DEBUG: Reuse of '<reg>'. ...`` path), while target ROMs assembled the Sony way show the jump
itself in the load's delay slot (``lw $v0,X; jr $ra``). This pass drops those delay nops where maspsx
semantics allow it:

SAFE (nop dropped): the following jump does NOT read the loaded register. The jump moves up into the
  load's delay slot; the load still completes before the jump target's first instruction executes.
  Covers ``j``/``jal`` with a label operand (they read nothing) and ``jr``/``jalr`` whose register is
  unrelated to the loaded one.

UNSAFE (nop kept, decision recorded): the jump reads the loaded register — ``lw $t9,X; nop; jr $t9``
  and maspsx's ``j $2`` register-jump spelling. Dropping the nop would jump through the stale value.

NEVER TOUCHED: conditional branches (a branch in a load's delay slot is invalid on the R3000), any
intervening label (it may be a branch target), more than one nop, any other instruction between the
load and the jump, and ``.ent``/``.end``/``.align``/``.globl`` boundaries. Blank lines, ``#`` comment
lines and ``.set`` directives (maspsx's EXPAND_AT ``noat``/``at`` pairs) are transparent to the
pattern. Untouched lines are byte-identical (round-trip identity); every decision (function, line,
load, jump, action, reason) is returned for the search record.

The pass is opt-in: it is registered under ``toolchains.asm_pass("maspsx-drop-delay-nops")``; the
default compile pipeline is unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LOAD_MNEMONICS = ("lw", "lh", "lhu", "lb", "lbu", "lwl", "lwr")
JUMP_MNEMONICS = ("j", "jal", "jr", "jalr")
BRANCH_MNEMONICS = ("beq", "bne", "bgtz", "blez", "bltz", "bgez", "beql", "bnel",
                    "bltzal", "bgezal", "bc0f", "bc0t", "bc1f", "bc1t")

_LOAD = re.compile(r"^\s*(lw|lh|lhu|lb|lbu|lwl|lwr)\s+\$(\w+)[,\s]")
_NOP = re.compile(r"^\s*nop\s*(?:#.*)?$")
_JUMP = re.compile(r"^\s*(j|jal|jr|jalr)\b")
_LABEL = re.compile(r"^\s*[A-Za-z_.$][\w.$]*:")
_SET = re.compile(r"^\s*\.set\b")
_COMMENT = re.compile(r"^\s*#")
_BLANK = re.compile(r"^\s*$")
_ENT = re.compile(r"^\s*\.ent\b")

# Register spellings in PSX asm: numeric ($2) and named ($v0) refer to the same register; the safety
# rule must compare canonical registers, so ``lw $2,X; nop; jr $v0`` is recognised as the unsafe case.
_REG_CANON = {}
for _n in range(32):
    _REG_CANON[f"${_n}"] = _n
for _n, _name in enumerate(("zero", "at", "v0", "v1", "a0", "a1", "a2", "a3",
                            "t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7",
                            "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7",
                            "t8", "t9", "k0", "k1", "gp", "sp", "fp", "ra")):
    _REG_CANON[f"${_name}"] = _n
_REG_CANON["$s8"] = 30  # $s8 is the other spelling of $fp


def norm_reg(token: str) -> int | None:
    """The canonical register number for ``$2`` / ``$v0`` / ``$ra`` spellings; None when not a register."""
    return _REG_CANON.get(token) if token.startswith("$") else _REG_CANON.get(f"${token}")


def _jump_read_regs(line: str, op: str) -> set[int]:
    """The registers a jump instruction reads: none for label j/jal, the operand register(s) for
    jr/jalr and for the ``j $2`` register-jump spelling maspsx emits."""
    operands = re.findall(r"\$(\w+)", line)
    if op in ("j", "jal") and len(operands) == 1:
        return {norm_reg(f"${operands[0]}")}
    if op in ("jr", "jalr"):
        return {r for r in (norm_reg(f"${o}") for o in operands) if r is not None}
    return set()


def _transparent(line: str) -> bool:
    """Lines that may sit between a load and its delay nop / the jump without breaking the pattern."""
    return bool(_BLANK.match(line) or _COMMENT.match(line) or _SET.match(line))


@dataclass(frozen=True)
class NopDropDecision:
    """One load -> nop -> jump observation: what was done and why (the search record)."""

    function: str
    line: int          # 1-based line of the nop in the input
    load: str
    jump: str
    action: str        # "dropped" | "kept"
    reason: str


@dataclass(frozen=True)
class NopDropReport:
    decisions: tuple[NopDropDecision, ...] = ()
    changed: bool = False

    @property
    def dropped(self) -> int:
        return sum(1 for d in self.decisions if d.action == "dropped")

    @property
    def kept(self) -> int:
        return sum(1 for d in self.decisions if d.action == "kept")

    def affected_functions(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for d in self.decisions:
            fn = out.setdefault(d.function or "?", {"dropped": 0, "kept": 0})
            fn[d.action] += 1
        return out


def drop_delay_nops(asm_text: str) -> tuple[str, NopDropReport]:
    """Drop load-delay nops before j/jr/jal where maspsx semantics allow. Pure text transform.

    Returns the new asm text and a report of every decision. The input is never reformatted: every
    line that survives is byte-identical to its input line."""
    lines = asm_text.split("\n")
    decisions: list[NopDropDecision] = []
    function = ""
    remove: set[int] = set()

    def classify(idx: int) -> str:
        """kind of the line at idx: load/nop/jump/branch/label/set/comment/blank/other."""
        line = lines[idx]
        if _ENT.match(line):
            return "ent"
        if _BLANK.match(line):
            return "blank"
        if _COMMENT.match(line):
            return "comment"
        if _SET.match(line):
            return "set"
        if _LABEL.match(line):
            return "label"
        if _LOAD.match(line):
            return "load"
        if _NOP.match(line):
            return "nop"
        m = _JUMP.match(line)
        if m:
            return f"jump:{m.group(1)}"
        m = re.match(r"^\s*([A-Za-z.]\w*)", line)
        if m and m.group(1) in BRANCH_MNEMONICS:
            return "branch"
        return "other"

    for i, line in enumerate(lines):
        em = re.match(r"\s*\.ent\s+(\w+)", line)
        if em:
            function = em.group(1)
            continue
        lm = _LOAD.match(line)
        if lm is None:
            continue
        loaded = norm_reg(lm.group(2))
        if loaded is None:
            continue
        j = i + 1
        while j < len(lines) and _transparent(lines[j]):
            j += 1
        if j >= len(lines) or classify(j) != "nop":
            continue
        k = j + 1
        while k < len(lines) and _transparent(lines[k]):
            k += 1
        if k >= len(lines):
            continue
        kind = classify(k)
        if not kind.startswith("jump:"):
            continue  # a branch or anything else after the nop: not this pass's business
        op = kind.split(":", 1)[1]
        reads = _jump_read_regs(lines[k], op)
        if loaded in reads:
            decisions.append(NopDropDecision(function, j + 1, line.strip(), lines[k].strip(),
                                             "kept", "the jump reads the loaded register: dropping the "
                                             "nop would jump through its stale value"))
        else:
            decisions.append(NopDropDecision(function, j + 1, line.strip(), lines[k].strip(),
                                             "dropped", "the jump does not read the loaded register: "
                                             "the jump occupies the load's delay slot"))
            remove.add(j)

    if not remove:
        return asm_text, NopDropReport(tuple(decisions), False)
    out = [line for i, line in enumerate(lines) if i not in remove]
    return "\n".join(out), NopDropReport(tuple(decisions), True)
