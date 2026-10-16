#!/usr/bin/env bash
# Smoke test: Unicorn emulates tiny MIPS BE, ARM Thumb and PowerPC snippets and checks result registers.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cat > "$T/u.py" <<'PY'
from unicorn import *
from unicorn.mips_const import *
from unicorn.arm_const import *
from unicorn.ppc_const import *
def run(arch, mode, code, setup, reg, start_off=0):
    mu = Uc(arch, mode); base = 0x10000
    mu.mem_map(base, 0x1000); mu.mem_write(base, code)
    for r, v in setup: mu.reg_write(r, v)
    mu.emu_start(base + start_off, base + len(code)); return mu.reg_read(reg)
# MIPS BE: addiu v0,zero,5 ; addiu v0,v0,7  -> 12
v = run(UC_ARCH_MIPS, UC_MODE_MIPS32 + UC_MODE_BIG_ENDIAN, bytes.fromhex('2402000524420007'), [], UC_MIPS_REG_V0)
assert v == 12, v; print('ok: mips v0 =', v)
# Thumb: adds r0,r0,r1 ; adds r0,r0,r2  (r0=1,r1=2,r2=3) -> 6   (start|1 selects Thumb)
v = run(UC_ARCH_ARM, UC_MODE_THUMB, bytes.fromhex('40188018'), [(UC_ARM_REG_R0,1),(UC_ARM_REG_R1,2),(UC_ARM_REG_R2,3)], UC_ARM_REG_R0, 1)
assert v == 6, v; print('ok: thumb r0 =', v)
# PPC32 BE: li r3,40 ; addi r3,r3,2  -> 42
v = run(UC_ARCH_PPC, UC_MODE_PPC32 + UC_MODE_BIG_ENDIAN, bytes.fromhex('386000283863 0002'.replace(' ','')), [], UC_PPC_REG_3)
assert v == 42, v; print('ok: ppc r3 =', v)
PY
"$HERE/.venv/bin/python" -I "$T/u.py"
echo PASS
