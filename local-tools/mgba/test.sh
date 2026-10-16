#!/usr/bin/env bash
# Smoke test: build a tiny homebrew GBA ROM (6*7 then SWI 3), run it in mgba-headless
# (exit code = r0 = 42), then drive the CLI debugger: breakpoint, registers, disassembly.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; M="$HERE/bin/mgba-headless"
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

# 1) headless run: stop at SWI 3, return r0 as exit code
set +e; perl -e 'alarm 30; exec @ARGV' "$M" -S 3 -R r0 rom.gba >run.log 2>&1; rc=$?; set -e
[ "$rc" -eq 42 ] || { echo "expected exit 42, got $rc"; cat run.log; exit 1; }

# 2) CLI debugger: break before the SWI, check r0 and disassembly
printf 'break 0x080000d2\ncontinue\ndisassemble 0x080000cc 4\nquit\n' > cmds
perl -e 'alarm 20; exec @ARGV' "$M" -d rom.gba < cmds > dbg.log 2>&1 || true
grep -q 'Hit breakpoint 1 at 0x080000D2' dbg.log
grep -q 'r0: 0000002A' dbg.log
grep -q '080000D0:  4348 .*mul r0, r1' dbg.log
echo "PASS mgba"
