#!/usr/bin/env bash
# Smoke test: assemble two tiny MIPS functions into an ELF object, hide their bytes at
# 0x400 inside a fake RAM dump, and check n64sym names both functions at the right
# addresses (directly from the .o, and via a .sig file built by n64sig at a new offset).
# Needs a MIPS GNU assembler: $MIPS_AS, or the decomp.wtf binutils-mips toolchain cache,
# or mips-linux-gnu-as on PATH.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CACHE="$HOME/Library/Application Support/decompnet/toolchains/binutils-mips"
AS="${MIPS_AS:-}"
[ -z "$AS" ] && [ -x "$CACHE/mips-ps2-decompals-as" ] && AS="$CACHE/mips-ps2-decompals-as"
[ -z "$AS" ] && AS="$(command -v mips-linux-gnu-as || true)"
[ -n "$AS" ] || { echo "FAIL: no MIPS assembler found (set MIPS_AS)"; exit 1; }
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cd "$T"
cat > f.s <<'ASM'
.set noreorder
.text
.globl my_unique_func
.type my_unique_func, @function
my_unique_func:
  addiu $sp, $sp, -24
  sw $ra, 20($sp)
  lui $t0, 0x1234
  ori $t0, $t0, 0x5678
  addu $v0, $a0, $t0
  sll $v0, $v0, 3
  xor $v0, $v0, $a1
  lw $ra, 20($sp)
  jr $ra
  addiu $sp, $sp, 24
.size my_unique_func, .-my_unique_func
.globl other_func
.type other_func, @function
other_func:
  addiu $sp, $sp, -8
  subu $v0, $a0, $a1
  srl $v0, $v0, 2
  andi $v0, $v0, 0xff
  jr $ra
  addiu $sp, $sp, 8
.size other_func, .-other_func
ASM
"$AS" -march=vr4300 -32 -EB f.s -o f.o
# pull the .text bytes out of the ELF (big-endian MIPS, section header walk)
python3 -I - <<'PY'
import struct
d = open("f.o", "rb").read()
shoff, = struct.unpack_from(">I", d, 0x20); shentsize, shnum, shstrndx = struct.unpack_from(">HHH", d, 0x2e)
secs = [struct.unpack_from(">IIIIII", d, shoff + i * shentsize) for i in range(shnum)]
stroff = secs[shstrndx][4]
name = lambda o: d[stroff + o:d.index(b"\0", stroff + o)]
text = next(d[s[4]:s[4] + s[5]] for s in secs if name(s[0]) == b".text")
open("dump1.bin", "wb").write(b"\0" * 0x400 + text + b"\0" * 0x100)
open("dump2.bin", "wb").write(b"\0" * 0x1000 + text[:0x28] + b"\0" * 0x40)  # only my_unique_func
PY
out=$("$HERE/bin/n64sym" dump1.bin -l f.o); echo "$out"
grep -qx '80000400 my_unique_func' <<<"$out"
grep -qx '80000428 other_func' <<<"$out"
"$HERE/bin/n64sig" -l f.o > mine.sig
grep -q '^my_unique_func 0x0028' mine.sig
out2=$("$HERE/bin/n64sym" dump2.bin -l mine.sig -f armips); echo "$out2"
grep -q '\.definelabel my_unique_func, 0x80001000$' <<<"$out2"
"$HERE/bin/n64sym" dump1.bin -s >/dev/null   # built-in signature DB loads
echo PASS
