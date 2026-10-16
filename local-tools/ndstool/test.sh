#!/usr/bin/env bash
# Smoke test: assemble tiny ARM9/ARM7 programs, pack them plus a NitroFS file into a
# .nds ROM with ndstool, then read the header back and extract everything again.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
nt="$here/bin/ndstool"
command -v arm-none-eabi-gcc >/dev/null || { echo "needs arm-none-eabi-gcc (brew)"; exit 1; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
printf '.arm\n.global _start\n_start:\n  mov r0, #0x2A\n  b _start\n' > a9.s
printf '.arm\n.global _start\n_start:\n  mov r1, #0x07\n  b _start\n' > a7.s
arm-none-eabi-gcc -nostdlib -Wl,-Ttext=0x02000000 -Wl,-e,_start a9.s -o a9.elf
arm-none-eabi-gcc -nostdlib -Wl,-Ttext=0x02380000 -Wl,-e,_start a7.s -o a7.elf
arm-none-eabi-objcopy -O binary a9.elf a9.bin
arm-none-eabi-objcopy -O binary a7.elf a7.bin
mkdir -p fs/data && echo hello-decomp > fs/data/hello.txt
"$nt" -c t.nds -9 a9.bin -7 a7.bin -r9 0x02000000 -e9 0x02000000 \
      -r7 0x02380000 -e7 0x02380000 -d fs -g DWTF 01 DECOMPWTF >/dev/null 2>&1
"$nt" -i t.nds > info.txt 2>&1
grep -q 'DECOMPWTF' info.txt
grep -Eq 'Game code[[:space:]]+DWTF' info.txt
grep -Eq 'ARM9 entry address[[:space:]]+0x2000000' info.txt
mkdir x
"$nt" -x t.nds -9 x/a9.bin -7 x/a7.bin -d x/fs >/dev/null 2>&1
cmp a9.bin x/a9.bin
cmp a7.bin x/a7.bin
[ "$(xxd -p -l 4 x/a9.bin)" = "2a00a0e3" ]   # mov r0, #0x2A
[ "$(cat x/fs/data/hello.txt)" = "hello-decomp" ]
echo "ndstool: PASS"
