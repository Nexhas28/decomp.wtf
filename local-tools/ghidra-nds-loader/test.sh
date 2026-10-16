#!/usr/bin/env bash
# Smoke test: install the NTRGhidra loader into a throwaway Ghidra user-settings dir (user.home is
# redirected, the real Ghidra install and ~/Library/ghidra are untouched), import a tiny self-built
# NDS-shaped ROM (valid header CRC, 12-byte ARM9 stub) with analyzeHeadless and check that the loader
# created the ARM9 memory map, labelled IO registers and made the _entry_arm9 function.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GHIDRA="${GHIDRA_INSTALL_DIR:-/Users/shanemcgovern/Documents/Codex/2026-10-14/i-want/work/ghidra-displaylink/ghidra_12.1.4_PUBLIC}"
export JAVA_HOME="${JAVA_HOME_21:-/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT; cd "$W"
VER="$(sed -n 's/^application.version=//p' "$GHIDRA/Ghidra/application.properties")"
EXT="$W/home/Library/ghidra/ghidra_${VER}_PUBLIC/Extensions"; mkdir -p "$EXT"
unzip -q "$HERE"/ghidra_*_NTRGhidra.zip -d "$EXT"
export JAVA_TOOL_OPTIONS="-Duser.home=$W/home"

# ARM9 stub (armv5te): mov r0,#0x40 ; mov r1,r0 ; infinite loop
cat > arm9.s <<'ASM'
    .arm
    .global _start
_start:
    mov r0, #0x40
    mov r1, r0
loop:
    b loop
ASM
arm-none-eabi-as -march=armv5te arm9.s -o arm9.o
arm-none-eabi-ld -Ttext=0x02000000 arm9.o -o arm9.elf
arm-none-eabi-objcopy -O binary arm9.elf arm9.bin

# Build a minimal NDS image: 0x200 header, ARM9 at 0x1000, ARM7 (4 bytes) at 0x2000, empty FAT/OVT.
python3 -I - <<'PY'
import struct
def crc16(d, c=0xFFFF):
    for b in d:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c
a9 = open('arm9.bin', 'rb').read()
rom = bytearray(0x4000)
rom[0:12] = b'SMOKETEST'.ljust(12, b'\0')
rom[12:16] = b'ATST'; rom[16:18] = b'01'
struct.pack_into('<8I', rom, 0x20, 0x1000, 0x02000000, 0x02000000, len(a9),
                                    0x2000, 0x02380000, 0x02380000, 4)
struct.pack_into('<4I', rom, 0x40, 0x3000, 0, 0x3000, 0)     # FNT, FAT (empty)
struct.pack_into('<4I', rom, 0x50, 0, 0, 0, 0)               # ARM9/ARM7 overlay tables (none)
struct.pack_into('<I', rom, 0x80, 0x200)                     # header size
struct.pack_into('<I', rom, 0x80 - 4, len(rom))              # rom size
rom[0x1000:0x1000 + len(a9)] = a9
rom[0x2000:0x2004] = struct.pack('<I', 0xE1A00000)           # ARM7: nop
struct.pack_into('<H', rom, 0x15E, crc16(bytes(rom[:0x15E])))
open('test.nds', 'wb').write(rom)
PY

mkdir scripts proj
cat > scripts/Dump.java <<'JAVA'
import ghidra.app.script.GhidraScript;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.listing.Function;
public class Dump extends GhidraScript {
  @Override public void run() throws Exception {
    println("LANG=" + currentProgram.getLanguageID());
    for (MemoryBlock b : currentProgram.getMemory().getBlocks()) println("BLOCK=" + b.getName() + "@" + b.getStart());
    for (Symbol s : currentProgram.getSymbolTable().getSymbols("REG_A_DISPCNT")) println("SYM=" + s.getName() + "@" + s.getAddress());
    for (Function f : currentProgram.getFunctionManager().getFunctions(true)) println("FUNC=" + f.getName() + "@" + f.getEntryPoint());
    println("INSN=" + currentProgram.getListing().getInstructionAt(toAddr(0x02000000)));
  }
}
JAVA
# The loader normally pops up GUI dialogs (commercial/homebrew, ARM7/ARM9, overlays); the patched build in
# this folder skips them in headless mode (defaults: homebrew, ARM9, all overlays).
"$GHIDRA/support/analyzeHeadless" "$W/proj" p -import test.nds -loader NTRGhidraLoader \
   -scriptPath "$W/scripts" -postScript Dump.java -noanalysis -deleteProject > log.txt 2>&1 || { tail -40 log.txt; exit 1; }
grep -q 'LANG=ARM:LE:32:v5t' log.txt || { tail -40 log.txt; echo "FAIL: language"; exit 1; }
grep -q 'BLOCK=ARM9_Main_Memory@02000000' log.txt
grep -q 'BLOCK=ARM9_IO_Ports@ARM9_IO_Ports::04000000' log.txt
grep -q 'BLOCK=VRAM_Engine_A_BG_VRAM@VRAM_Engine_A_BG_VRAM::06000000' log.txt
grep -q 'SYM=REG_A_DISPCNT@04000000' log.txt
grep -q 'FUNC=_entry_arm9@02000000' log.txt
grep -q 'INSN=mov r0,#0x40' log.txt
grep -oE "(LANG|BLOCK|SYM|FUNC|INSN)=[^ ].*" log.txt | sed 's/ (GhidraScript).*//'
echo "PASS ghidra-nds-loader"
