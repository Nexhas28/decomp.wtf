#!/usr/bin/env bash
# Smoke test for the ido-static-recomp `recomp` tool (no proprietary IDO binaries used).
# 1. Assemble + link a tiny self-written big-endian MIPS program shaped like an IRIX dynamic
#    executable (main calls add3(39); add3 returns a0+3), using Homebrew llvm@21 / lld@21.
# 2. Patch it the way IRIX ld lays things out (functions in SHN_MIPS_TEXT, exported symbols in
#    the global GOT range), since recomp only reads .dynsym.
# 3. Run recomp to translate it to C, check f_main/f_add3 exist, compile the C with the
#    bundled runtime (libc_impl.c) and run it natively: exit code must be 42.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LLVM="${LLVM_BIN:-/opt/homebrew/opt/llvm@21/bin}"
LLD="${LLD_BIN:-/opt/homebrew/opt/lld@21/bin}"
for x in "$LLVM/llvm-mc" "$LLD/ld.lld" "$HERE/bin/recomp"; do [ -x "$x" ] || { echo "FAIL missing $x"; exit 1; }; done
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cd "$T"

cat > t.s <<'ASM'
    .set noreorder
    .text
    .globl main
    .type main,@function
    .ent main
main:
    addiu $sp, $sp, -24
    sw $ra, 20($sp)
    lw $t0, %got(__Argc)($gp)
    lw $t1, %got(__Argv)($gp)
    jal add3
    li $a0, 39
    lw $ra, 20($sp)
    jr $ra
    addiu $sp, $sp, 24
    .end main
    .globl add3
    .type add3,@function
    .ent add3
add3:
    jr $ra
    addiu $v0, $a0, 3
    .end add3
    .data
    .globl __Argc
    .type __Argc,@object
    .size __Argc,4
__Argc: .word 0
    .globl __Argv
    .type __Argv,@object
    .size __Argv,4
__Argv: .word 0
ASM
printf '\t.text\n\t.globl stub\nstub:\n\tjr $ra\n\tnop\n' > stub.s

cat > irixify.py <<'PY'
import struct, sys
p = sys.argv[1]; d = bytearray(open(p, 'rb').read())
shoff, = struct.unpack_from('>I', d, 0x20); shentsize, shnum = struct.unpack_from('>HH', d, 0x2e)
secs = [struct.unpack_from('>IIIIIIIIII', d, shoff + i * shentsize) for i in range(shnum)]
n = 0
for s in secs:
    if s[1] == 11:  # SHT_DYNSYM: defined functions -> SHN_MIPS_TEXT
        for off in range(s[4], s[4] + s[5], 16):
            info = d[off + 12]; shndx, = struct.unpack_from('>H', d, off + 14)
            if info & 0xf == 2 and shndx not in (0, 0xfff1, 0xfff2):
                struct.pack_into('>H', d, off + 14, 0xff01); n += 1
for s in secs:
    if s[1] == 6:  # SHT_DYNAMIC: start global GOT at dynsym index argv[2]
        tags = {}
        for off in range(s[4], s[4] + s[5], 8):
            tag, val = struct.unpack_from('>II', d, off); tags[tag] = (off, val)
        gs_off, gs = tags[0x70000013]; lg_off, lg = tags[0x7000000a]
        new = int(sys.argv[2]); shift = gs - new
        struct.pack_into('>I', d, gs_off + 4, new); struct.pack_into('>I', d, lg_off + 4, lg - shift)
open(p, 'wb').write(d); print('patched funcs:', n)
PY

"$LLVM/llvm-mc" -triple=mips-unknown-linux-gnu -mcpu=mips2 -filetype=obj t.s -o t.o
"$LLVM/llvm-mc" -triple=mips-unknown-linux-gnu -mcpu=mips2 -filetype=obj stub.s -o stub.o
"$LLD/ld.lld" -m elf32btsmip -shared -soname libstub.so stub.o -o libstub.so
"$LLD/ld.lld" -m elf32btsmip -s --export-dynamic --dynamic-linker=/usr/lib/libc.so.1 \
  -e main -Ttext=0x10000100 t.o libstub.so -o t.elf
python3 -I irixify.py t.elf 2

"$HERE/bin/recomp" t.elf > t.c
grep -q 'static uint32_t f_main(uint8_t \*mem' t.c || { echo "FAIL no f_main in output"; exit 1; }
grep -q 'v0 = f_add3(mem, sp, a0);' t.c || { echo "FAIL no call to f_add3"; exit 1; }
echo "recomp produced $(wc -l < t.c | tr -d ' ') lines of C"

R="$HERE/runtime"
cc -O1 -w -I"$R" -DIDO71 -DPACKAGE_VERSION='"smoke"' -DDATETIME='"smoke"' \
  t.c "$R/libc_impl.c" "$R/version_info.c" -o tnative
set +e; ./tnative; rc=$?; set -e
echo "recompiled program exit code: $rc"
[ "$rc" -eq 42 ] || { echo "FAIL expected 42"; exit 1; }
echo PASS
