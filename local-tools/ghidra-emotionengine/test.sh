#!/usr/bin/env bash
# Smoke test: install the PS2 Emotion Engine extension into a throwaway Ghidra user-settings dir
# (user.home is redirected; the real Ghidra install and ~/Library/ghidra are untouched), import a
# tiny hand-made blob of R5900 machine code with analyzeHeadless using the extension's
# r5900:LE:32:default language, and check EE-only instructions (lq/sq/paddw) disassemble correctly.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GHIDRA="${GHIDRA_INSTALL_DIR:-/Users/shanemcgovern/Documents/Codex/2026-10-14/i-want/work/ghidra-displaylink/ghidra_12.1.4_PUBLIC}"
export JAVA_HOME="${JAVA_HOME_21:-/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT; cd "$W"
VER="$(sed -n 's/^application.version=//p' "$GHIDRA/Ghidra/application.properties")"
EXT="$W/home/Library/ghidra/ghidra_${VER}_PUBLIC/Extensions"; mkdir -p "$EXT"
unzip -q "$HERE"/ghidra_*_ghidra-emotionengine-reloaded.zip -d "$EXT"
export JAVA_TOOL_OPTIONS="-Duser.home=$W/home"

# Little-endian R5900 words: lq a0,0x10(sp); paddw v0,a0,a1; sq v0,0x20(sp); jr ra; nop
printf '\x10\x00\xa4\x7b\x08\x10\x85\x70\x20\x00\xa2\x7f\x08\x00\xe0\x03\x00\x00\x00\x00' > ee.bin

mkdir scripts proj
cat > scripts/Dis.java <<'JAVA'
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Instruction;
public class Dis extends GhidraScript {
  @Override public void run() throws Exception {
    println("LANG=" + currentProgram.getLanguageID());
    disassemble(currentProgram.getMinAddress());
    for (Instruction i : currentProgram.getListing().getInstructions(true)) println("INSN=" + i.getAddress() + " " + i);
  }
}
JAVA
"$GHIDRA/support/analyzeHeadless" "$W/proj" p -import ee.bin -loader BinaryLoader \
   -processor r5900:LE:32:default -scriptPath "$W/scripts" -postScript Dis.java -noanalysis -deleteProject > log.txt 2>&1 || { tail -40 log.txt; exit 1; }
grep -q 'LANG=r5900:LE:32:default' log.txt || { tail -40 log.txt; exit 1; }
grep -qE 'INSN=0+ lq a0,0x10\(sp\)' log.txt || { grep INSN log.txt; exit 1; }
grep -qE 'INSN=0+4 paddw v0,a0,a1' log.txt || { grep INSN log.txt; exit 1; }
grep -qE 'INSN=0+8 sq v0,0x20\(sp\)' log.txt || { grep INSN log.txt; exit 1; }
grep -oE "(LANG|INSN)=.*" log.txt | sed 's/ (GhidraScript).*//'
echo "PASS ghidra-emotionengine"
