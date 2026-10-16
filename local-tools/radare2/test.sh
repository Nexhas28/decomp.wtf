#!/usr/bin/env bash
# Smoke test: radare2 disasm/asm for MIPS, Thumb, PPC, SM83 + ESIL emulation + ELF function analysis.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; B="$HERE/bin"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fail(){ echo "FAIL: $*"; exit 1; }
chk(){ local got; got="$("${@:2}" 2>&1)"; [[ "$got" == *"$1"* ]] || fail "expected '$1' from: ${*:2} -> $got"; echo "ok: $1"; }
chk "addiu sp, sp, -0x18" "$B/rasm2" -a mips -b 32 -e -d 27bdffe8   # MIPS BE (N64/PS2-style)
chk "push {r4, lr}"       "$B/rasm2" -a arm -b 16 -d 10b5           # Thumb (GBA/NDS)
chk "stwu r1, -0x10(r1)"  "$B/rasm2" -a ppc -b 32 -e -d 9421fff0    # PowerPC (GC/Wii)
chk "call 0x0150"         "$B/rasm2" -a gb -d cd5001                # SM83 (Game Boy)
chk "03e00008"            "$B/rasm2" -a mips -b 32 -e 'jr ra'       # assemble MIPS
chk "1eff2fe1"            "$B/rasm2" -a arm -b 32 'bx lr'           # assemble ARM
# ESIL emulation: addiu v0,zero,5 ; addiu v0,v0,7  => v0 = 0xc
chk "0x0000000c" "$B/radare2" -q -a mips -b 32 -e scr.color=0 \
  -c 'e cfg.bigendian=true; wx 2402000524420007; aei; aeim; aeip; aes; aes; ar v0' malloc://64
# Function analysis on a self-built Thumb object (only if the cross compiler is available)
if command -v arm-none-eabi-gcc >/dev/null; then
  echo 'int add3(int a,int b,int c){return a+b+c;}' > "$T/t.c"
  arm-none-eabi-gcc -O2 -mthumb -mcpu=arm7tdmi -c "$T/t.c" -o "$T/t.o"
  chk "r0, r0, r2" "$B/radare2" -q -e scr.color=0 -c 'aaa; pdf @ sym.add3' "$T/t.o"
else echo "skip: arm-none-eabi-gcc not found"; fi
echo PASS
