#!/usr/bin/env bash
# Smoke test: set up a permuter directory by hand (base.c, compile.sh using IDO, target.o),
# then (1) a mismatching target: --debug must report a non-zero base score,
# (2) the matching target: the permuter must report score 0 and stop (--stop-on-zero).
# Needs IDO ($IDO_CC, default ~/Decomp/tools/vendor/compilers/ido-7.1/cc) and MIPS binutils
# ($MIPS_AS with objdump next to it, default the decomp.wtf binutils-mips toolchain cache).
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
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cd "$T"
# the permuter finds objdump on PATH under its usual cross name
mkdir bin; ln -s "${AS%as}objdump" bin/mips-linux-gnu-objdump; export PATH="$T/bin:$PATH"
# run a command with a hard time limit (macOS has no `timeout`)
limit() { perl -e 'alarm shift; exec @ARGV' "$@"; }
mkdir d
cat > d/compile.sh <<SH
#!/bin/bash
exec "$IDO" -c -G 0 -non_shared -Xcpluscomm -O2 -mips2 "\$@"
SH
chmod +x d/compile.sh
cat > d/settings.toml <<TOML
func_name = "func"
compiler_type = "ido"
TOML
echo 'int func(int x, int y) { return x * 3 + y; }' > d/base.c
# (1) target built from different source -> mismatch
echo 'int func(int x, int y) { return x * 5 + y; }' > other.c
d/compile.sh other.c -o d/target.o
out1=$(limit 60 "$HERE/run" d --debug 2>&1); echo "$out1" | tail -3
score=$(sed -n 's/.*base score = \([0-9]*\).*/\1/p' <<<"$out1" | head -1)
[ -n "$score" ] && [ "$score" -gt 0 ] || { echo "FAIL: expected non-zero base score"; exit 1; }
# (2) target built from the same source -> score 0, permuter stops
d/compile.sh d/base.c -o d/target.o
out2=$(limit 60 "$HERE/run" d --stop-on-zero -j1 2>&1); echo "$out2" | tail -3
grep -q 'base score = 0' <<<"$out2"
grep -q 'Found zero score' <<<"$out2"
echo PASS
