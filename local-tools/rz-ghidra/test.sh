#!/usr/bin/env bash
# Smoke test: rz-ghidra decompiles add3() from tiny self-built MIPS BE / PowerPC / ARM ELFs via rizin `pdg`.
# Requires ../rizin (local-tools/rizin) which this plugin was built against.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
printf '__attribute__((noinline)) int add3(int a,int b,int c){return a+b+c;}\nint main(int x,char**v){return add3(x,2,3);}\n' > "$T/m.c"
CL=""; for c in /opt/homebrew/opt/llvm/bin/clang /opt/homebrew/opt/llvm@21/bin/clang; do [ -x "$c" ] && CL="$c" && break; done
LLD="$(command -v ld.lld || ls /opt/homebrew/opt/lld*/bin/ld.lld 2>/dev/null | head -1 || true)"
n=0
dec(){ local out; out="$("$HERE/run" -q -e scr.color=0 -c 'aaa; s sym.add3; pdg' "$1" 2>&1)"
  echo "$out" | grep -Eq 'return [a-z_0-9]+ \+ [a-z_0-9]+ \+ [a-z_0-9]+;' || { echo "FAIL: $2 decompile"; echo "$out"; exit 1; }
  echo "ok: $2 -> $(echo "$out" | grep -E 'return ' | xargs)"; n=$((n+1)); }
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
