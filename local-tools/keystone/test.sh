#!/usr/bin/env bash
# Smoke test: Keystone assembles MIPS/Thumb/ARM/PPC via kstool CLI and the Python binding; bytes are checked.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fail(){ echo "FAIL: $*"; exit 1; }
chk(){ local got; got="$("${@:2}" 2>&1)"; [[ "$got" == *"$1"* ]] || fail "expected '$1' from: ${*:2} -> $got"; echo "ok: $1"; }
chk "03 e0 00 08" "$HERE/bin/kstool" mipsbe 'jr $ra'
chk "70 47"       "$HERE/bin/kstool" thumb 'bx lr'
chk "4e 80 00 20" "$HERE/bin/kstool" ppc32be 'blr'
cat > "$T/k.py" <<'PY'
from keystone import *
def a(arch, mode, s): return bytes(Ks(arch, mode).asm(s)[0]).hex()
assert a(KS_ARCH_MIPS, KS_MODE_MIPS32+KS_MODE_BIG_ENDIAN, 'addiu $sp, $sp, -24') == '27bdffe8'
assert a(KS_ARCH_ARM, KS_MODE_THUMB, 'push {r4, lr}') == '10b5'
assert a(KS_ARCH_ARM, KS_MODE_ARM, 'bx lr') == '1eff2fe1'
assert a(KS_ARCH_PPC, KS_MODE_PPC32+KS_MODE_BIG_ENDIAN, 'stwu 1, -16(1)') == '9421fff0'
print('ok: python binding')
PY
"$HERE/.venv/bin/python" -I "$T/k.py"
echo PASS
