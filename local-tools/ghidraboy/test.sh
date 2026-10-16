#!/usr/bin/env bash
# Smoke test: install GhidraBoy into a throwaway Ghidra user-settings dir (user.home redirected;
# the real Ghidra install and ~/Library/ghidra are untouched), build a tiny Game Boy ROM with
# rgbds, import it with analyzeHeadless + the "Game Boy" loader and disassemble SM83 code.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GHIDRA="${GHIDRA_INSTALL_DIR:-/Users/shanemcgovern/Documents/Codex/2026-10-14/i-want/work/ghidra-displaylink/ghidra_12.1.4_PUBLIC}"
export JAVA_HOME="${JAVA_HOME_21:-/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT; cd "$W"
VER="$(sed -n 's/^application.version=//p' "$GHIDRA/Ghidra/application.properties")"
EXT="$W/home/Library/ghidra/ghidra_${VER}_PUBLIC/Extensions"; mkdir -p "$EXT"
unzip -q "$HERE"/ghidra_*_GhidraBoy.zip -d "$EXT"
export JAVA_TOOL_OPTIONS="-Duser.home=$W/home"

cat > rom.asm <<'ASM'
SECTION "entry", ROM0[$100]
    nop
    jp Main
    ds $150 - @, 0          ; header area, filled in by rgbfix
SECTION "main", ROM0[$150]
Main:
    ld a, $2A
    ld [$C000], a
    ld hl, $FF40
    halt
    jr Main
ASM
rgbasm -o rom.o rom.asm
rgblink -o rom.gb rom.o
rgbfix -v -p 0 -t TESTROM rom.gb

mkdir scripts proj
cat > scripts/Dump.java <<'JAVA'
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.mem.MemoryBlock;
public class Dump extends GhidraScript {
  @Override public void run() throws Exception {
    println("LANG=" + currentProgram.getLanguageID());
    for (MemoryBlock b : currentProgram.getMemory().getBlocks()) println("BLOCK=" + b.getName() + "@" + b.getStart());
    disassemble(toAddr(0x150));
    Instruction i = getInstructionAt(toAddr(0x150));
    for (int n = 0; n < 5 && i != null; n++, i = i.getNext()) println("INSN=" + i.getAddress() + ":" + i.toString().replace(' ', '_'));
  }
}
JAVA
"$GHIDRA/support/analyzeHeadless" "$W/proj" p -import rom.gb -loader GameBoyLoader \
   -scriptPath "$W/scripts" -postScript Dump.java -noanalysis -deleteProject > log.txt 2>&1 || { tail -40 log.txt; exit 1; }
grep -oE "(LANG|BLOCK|INSN)=[^ ]+" log.txt
grep -q 'LANG=SM83:LE:16:default' log.txt
grep -q 'BLOCK=rom@0000' log.txt
grep -qi 'INSN=0150:LD_A,0x2a' log.txt
grep -qi 'INSN=.*HALT' log.txt
echo "PASS ghidraboy"
