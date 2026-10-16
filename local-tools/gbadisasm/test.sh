#!/usr/bin/env bash
# Smoke test: assemble a tiny ARM+Thumb GBA ROM, disassemble it with gbadisasm, check output.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT; cd "$W"
cat > rom.s <<'ASM'
    .syntax unified
    .arm
    .global _start
_start:
    b start_arm
    .fill 0xB2-4, 1, 0
    .byte 0x96
    .fill 0xC0-0xB3, 1, 0
start_arm:
    ldr r0, =thumb_main+1
    bx r0
    .pool
    .thumb
    .thumb_func
thumb_main:
    movs r0, #6
    movs r1, #7
    muls r0, r1
    swi 3
loop:
    b loop
ASM
arm-none-eabi-as -mcpu=arm7tdmi rom.s -o rom.o
arm-none-eabi-ld -Ttext=0x08000000 rom.o -o rom.elf
arm-none-eabi-objcopy -O binary rom.elf rom.gba
printf 'arm_func 0x80000c0 StartArm\nthumb_func 0x80000cc ThumbMain\n' > rom.cfg
"$HERE/bin/gbadisasm" rom.gba -c rom.cfg > out.s
grep -q 'thumb_func_start ThumbMain' out.s
grep -q 'muls r0, r1, r0' out.s
grep -q 'svc #3' out.s
grep -q 'ldr r0, .* @ =ThumbMain' out.s
echo "PASS gbadisasm"
