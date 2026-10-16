#!/usr/bin/env bash
# Smoke test: pypcode (Ghidra SLEIGH in Python) disassembles MIPS BE / Thumb / PPC bytes and lifts them to P-code.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cat > "$T/p.py" <<'PY'
from pypcode import Context, OpCode
def dis(lang, code, **kw):
    ctx = Context(lang); d = ctx.disassemble(code, 0x80000000, **kw)
    return ctx, [" ".join(f"{i.mnem} {i.body}".replace(","," , ").split()).replace(" , ",", ") for i in d.instructions]
ctx, ins = dis("MIPS:BE:32:default", bytes.fromhex("27bdffe803e0000800000000"))
assert ins[0] == "addiu sp, sp, -0x18" and ins[1].startswith("jr ra"), ins; print("ok: mips", ins)
ops = [op.opcode for op in ctx.translate(bytes.fromhex("27bdffe8"), 0x80000000).ops]
assert OpCode.INT_ADD in ops, ops; print("ok: mips pcode", [o.name for o in ops])
ctx, ins = dis("ARM:LE:32:v4t", bytes.fromhex("1eff2fe1"))
assert ins[0] == "bx lr", ins; print("ok: arm", ins)
ctx, ins = dis("PowerPC:BE:32:default", bytes.fromhex("9421fff04e800020"))
assert ins[0] == "stwu r1, -0x10(r1)" and ins[1] == "blr", ins; print("ok: ppc", ins)
PY
"$HERE/.venv/bin/python" -I "$T/p.py"
echo PASS
