#!/usr/bin/env bash
# Smoke test for mwccgap. The real MWCC compiler is proprietary and not shipped, so this uses
# fake-mwcc.py (a tiny stand-in that emits a MWCC-shaped MIPS ELF) as --mwcc-path, and a real
# GNU MIPS assembler (decomp.wtf binutils-mips cache, $MIPS_AS, or mips-linux-gnu-as) for the asm.
# It runs the full mwccgap pipeline: compile C -> replace INCLUDE_ASM with nops -> compile ->
# assemble the .s -> transplant the real instructions into the object. Then disassembles and checks:
#   - the plain C function (add_two) is untouched (addiu v0,zero,2)
#   - the INCLUDE_ASM function (hand_func) holds the hand-written instructions, not nops
# NOTE: mwccgap reads C from stdin when stdin is not a TTY, so the runs below pass </dev/null.
# Also checks a missing .s file makes mwccgap fail, and runs the upstream unit tests.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CACHE="$HOME/Library/Application Support/decompnet/toolchains/binutils-mips"
AS="${MIPS_AS:-}"
[ -z "$AS" ] && [ -x "$CACHE/mips-ps2-decompals-as" ] && AS="$CACHE/mips-ps2-decompals-as"
[ -z "$AS" ] && AS="$(command -v mips-linux-gnu-as || true)"
[ -n "$AS" ] || { echo "FAIL: no MIPS assembler found (set MIPS_AS)"; exit 1; }
OBJDUMP="${AS%as}objdump"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/asm"
cat > "$T/macro.inc" <<'INC'
.macro glabel label
    .global \label
    .type \label, @function
\label:
.endm
.macro endlabel label
    .size \label, . - \label
.endm
INC
cat > "$T/asm/hand_func.s" <<'S'
.set noreorder
.section .text
glabel hand_func
    addiu $a0, $a0, 1
    jr $ra
    addiu $v0, $a0, 7
endlabel hand_func
S
cat > "$T/test.c" <<'C'
#define INCLUDE_ASM(FOLDER, NAME)
int add_two(void) { return 2; }
INCLUDE_ASM("asm", hand_func);
C
cd "$T"
"$HERE/run" test.c out.o --mwcc-path "$HERE/fake-mwcc.py" --as-path "$AS" --as-march mips32 \
    --macro-inc-path "$T/macro.inc" --asm-dir-prefix "$T" </dev/null
test -s out.o
dis="$("$OBJDUMP" -d -M no-aliases out.o)"; echo "$dis"
# the C function is untouched
awk '/<add_two>:/{f=1;next} /<hand_func>:/{f=0} f' <<<"$dis" | grep -q 'addiu.*v0,zero,2'
# the INCLUDE_ASM function has the hand-written code in place of nops
awk '/<hand_func>:/{f=1;next} f' <<<"$dis" > hand.txt
grep -q 'addiu.*a0,a0,1' hand.txt
grep -q 'jr.*ra' hand.txt
grep -q 'addiu.*v0,a0,7' hand.txt
! grep -q 'nop\|sll.*zero,zero,0' hand.txt
echo "transplant OK"
# error path: missing .s must fail and leave no object
printf 'INCLUDE_ASM("asm", nope);\n' > bad.c
if "$HERE/run" bad.c bad.o --mwcc-path "$HERE/fake-mwcc.py" --as-path "$AS" --as-march mips32 --asm-dir-prefix "$T" </dev/null 2>err.txt; then
  echo "FAIL: missing .s should have failed"; exit 1; fi
grep -q "does not exist" err.txt && test ! -e bad.o
echo "missing-asm error path OK"
# upstream unit tests (run from the bundled source copy)
(cd "$HERE/src" && python3 -I -m unittest discover -s tests -t . 2>&1 | tail -3)
echo "PASS: mwccgap"
