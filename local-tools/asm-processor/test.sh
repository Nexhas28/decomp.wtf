#!/usr/bin/env bash
# Smoke test: compile a C file that mixes a normal C function with a GLOBAL_ASM function,
# using IDO + asm-processor, and check both functions land in .text with the hand-written
# asm bytes intact. Needs IDO (recompiled, native) and a MIPS GNU assembler:
#   $IDO_CC  (default: ~/Decomp/tools/vendor/compilers/ido-7.1/cc, then ido-5.3/cc)
#   $MIPS_AS (default: decomp.wtf binutils-mips toolchain cache, then mips-linux-gnu-as)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CACHE="$HOME/Library/Application Support/decompnet/toolchains/binutils-mips"
IDO="${IDO_CC:-}"
for c in "$HOME/Decomp/tools/vendor/compilers/ido-7.1/cc" "$HOME/Decomp/tools/vendor/compilers/ido-5.3/cc"; do
  [ -z "$IDO" ] && [ -x "$c" ] && IDO="$c"
done
[ -n "$IDO" ] || { echo "FAIL: IDO cc not found (set IDO_CC)"; exit 1; }
AS="${MIPS_AS:-}"
[ -z "$AS" ] && [ -x "$CACHE/mips-ps2-decompals-as" ] && AS="$CACHE/mips-ps2-decompals-as"
[ -z "$AS" ] && AS="$(command -v mips-linux-gnu-as || true)"
[ -n "$AS" ] || { echo "FAIL: no MIPS assembler found (set MIPS_AS)"; exit 1; }
OBJDUMP="${AS%as}objdump"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cd "$T"
# paths with spaces break the IDO driver / asm-processor arg passing, so symlink them
ln -s "$AS" mips-as
cat > in.c <<'C'
int c_func(int x) { return x * 3 + 1; }

GLOBAL_ASM(
glabel asm_func
lui     $v0, 0xdead
jr      $ra
ori     $v0, $v0, 0xbeef
)

int c_func2(int y) { return y - 7; }
C
"$HERE/bin/asm-processor" --asm-prelude "$HERE/prelude.inc" "$IDO" -- ./mips-as -march=vr4300 -32 -EB -- \
  -c -G 0 -non_shared -Xcpluscomm -O2 -mips2 -o out.o in.c
dis=$("$OBJDUMP" -d out.o); echo "$dis"
grep -q '<c_func>:' <<<"$dis"
grep -q '<asm_func>:' <<<"$dis"
grep -q '<c_func2>:' <<<"$dis"
grep -q '3c02dead' <<<"$dis"
grep -q '3442beef' <<<"$dis"
echo PASS
