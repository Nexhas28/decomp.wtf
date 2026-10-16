#!/usr/bin/env bash
# Smoke test: install the GBA loader into a throwaway Ghidra user-settings dir (user.home is
# redirected, the real Ghidra install and ~/Library/ghidra are untouched), import a tiny
# self-built GBA ROM with analyzeHeadless and check the loader created the GBA memory map + IO labels.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GHIDRA="${GHIDRA_INSTALL_DIR:-/Users/shanemcgovern/Documents/Codex/2026-10-14/i-want/work/ghidra-displaylink/ghidra_12.1.4_PUBLIC}"
export JAVA_HOME="${JAVA_HOME_21:-/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT; cd "$W"
VER="$(sed -n 's/^application.version=//p' "$GHIDRA/Ghidra/application.properties")"
EXT="$W/home/Library/ghidra/ghidra_${VER}_PUBLIC/Extensions"; mkdir -p "$EXT"
unzip -q "$HERE"/ghidra_*_gba-ghidra-loader.zip -d "$EXT"
export JAVA_TOOL_OPTIONS="-Duser.home=$W/home"

cat > rom.s <<'ASM'
    .syntax unified
    .arm
    .global _start
_start:
    b start_arm
    .byte 0x24,0xFF,0xAE,0x51,0x69,0x9A,0xA2,0x21   @ header signature bytes the loader checks
    .fill 0xB2-12, 1, 0
    .byte 0x96
    .fill 0xC0-0xB3, 1, 0
start_arm:
    ldr r0, =0x04000000      @ DISPCNT
    mov r1, #0x80
    strh r1, [r0]
loop:
    b loop
    .pool
    .fill 0x100, 1, 0
ASM
arm-none-eabi-as -mcpu=arm7tdmi rom.s -o rom.o
arm-none-eabi-ld -Ttext=0x08000000 rom.o -o rom.elf
arm-none-eabi-objcopy -O binary rom.elf rom.gba

mkdir scripts proj
cat > scripts/Dump.java <<'JAVA'
import ghidra.app.script.GhidraScript;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Symbol;
public class Dump extends GhidraScript {
  @Override public void run() throws Exception {
    println("LANG=" + currentProgram.getLanguageID());
    for (MemoryBlock b : currentProgram.getMemory().getBlocks()) println("BLOCK=" + b.getName() + "@" + b.getStart());
    for (Symbol s : currentProgram.getSymbolTable().getSymbols("DISPCNT")) println("SYM=" + s.getName() + "@" + s.getAddress());
  }
}
JAVA
"$GHIDRA/support/analyzeHeadless" "$W/proj" p -import rom.gba -loader GBALoader \
   -scriptPath "$W/scripts" -postScript Dump.java -noanalysis -deleteProject > log.txt 2>&1 || { tail -40 log.txt; exit 1; }
grep -q 'LANG=ARM:LE:32:v4t' log.txt
grep -q 'BLOCK=rom@08000' log.txt
grep -q 'BLOCK=VRAM@06000000' log.txt
grep -q 'SYM=DISPCNT@04000000' log.txt
grep -oE "(LANG|BLOCK|SYM)=[^ ]+" log.txt
echo "PASS gba-ghidra-loader"
