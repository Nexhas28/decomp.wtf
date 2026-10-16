#!/usr/bin/env bash
# Smoke test: decompile tiny self-built MIPS BE / PowerPC / ARM ELFs to C and check the recovered function.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
printf '__attribute__((noinline)) int add3(int a,int b,int c){return a+b+c;}\nint main(int x,char**v){return add3(x,2,3);}\n' > "$T/m.c"
CL=""; for c in /opt/homebrew/opt/llvm/bin/clang /opt/homebrew/opt/llvm@21/bin/clang; do [ -x "$c" ] && CL="$c" && break; done
LLD="$(command -v ld.lld || ls /opt/homebrew/opt/lld*/bin/ld.lld 2>/dev/null | head -1 || true)"
n=0
dec(){ "$HERE/run" "$1" -o "$1.c" > "$1.log" 2>&1 || { echo "FAIL: retdec on $1"; tail "$1.log"; exit 1; }
  grep -q 'int32_t add3(int32_t a1, int32_t a2, int32_t a3)' "$1.c" && grep -q 'return add3(argc, 2, 3);' "$1.c" \
    || { echo "FAIL: unexpected C for $1"; cat "$1.c"; exit 1; }; echo "ok: $2"; n=$((n+1)); }
if [ -n "$CL" ] && [ -n "$LLD" ]; then
  for t in mips-unknown-elf:mips powerpc-unknown-elf:ppc; do
    "$CL" --target="${t%%:*}" -O1 -c "$T/m.c" -o "$T/${t##*:}.o"; "$LLD" -e main "$T/${t##*:}.o" -o "$T/${t##*:}.elf"; dec "$T/${t##*:}.elf" "${t##*:}"
  done
fi
if command -v arm-none-eabi-gcc >/dev/null; then
  arm-none-eabi-gcc -O1 -marm -nostdlib -Wl,-e,main -o "$T/arm.elf" "$T/m.c"; dec "$T/arm.elf" arm
fi
[ "$n" -gt 0 ] || { echo "FAIL: no cross compiler found to build test inputs"; exit 1; }
echo PASS
