#!/usr/bin/env bash
# Smoke test: install the N64 loader into a throwaway Ghidra user-settings dir (user.home is
# redirected, the real Ghidra install and ~/Library/ghidra are untouched), import two tiny
# self-built N64 ROM-shaped files (big-endian .z64 and byte-swapped .v64) with analyzeHeadless and
# check the loader fixed the endianness, created the N64 memory map and mapped the code at its load address.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GHIDRA="${GHIDRA_INSTALL_DIR:-/Users/shanemcgovern/Documents/Codex/2026-10-14/i-want/work/ghidra-displaylink/ghidra_12.1.4_PUBLIC}"
export JAVA_HOME="${JAVA_HOME_21:-/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT; cd "$W"
VER="$(sed -n 's/^application.version=//p' "$GHIDRA/Ghidra/application.properties")"
EXT="$W/home/Library/ghidra/ghidra_${VER}_PUBLIC/Extensions"; mkdir -p "$EXT"
unzip -q "$HERE"/ghidra_*_N64LoaderWV.zip -d "$EXT"
export JAVA_TOOL_OPTIONS="-Duser.home=$W/home"

# Hand-made ROM-shaped blobs (no game data): 0x1000 header+bootcode area, then 3 MIPS instructions
# at ROM offset 0x1000 which the loader maps to the header load address 0x80000400.
python3 -I - "$W" <<'PY'
import struct, sys
w = sys.argv[1]
rom = bytearray(0x2000)
rom[0:4] = bytes.fromhex("80371240")            # big-endian (.z64) magic
struct.pack_into(">I", rom, 8, 0x80000400)      # load / entry address
rom[0x20:0x20 + 12] = b"SMOKE TEST  "           # title
rom[0x3C:0x3E] = b"ST"                          # game code
rom[0x3E] = ord("E")                            # region
code = [0x27BDFFF8, 0x03E00008, 0x00000000]     # addiu sp,sp,-8 ; jr ra ; nop
for i, c in enumerate(code):
    struct.pack_into(">I", rom, 0x1000 + 4 * i, c)
open(w + "/test.z64", "wb").write(rom)
sw = bytearray(rom)
for i in range(0, len(sw), 2):                  # .v64 = every 16-bit pair byte-swapped
    sw[i], sw[i + 1] = sw[i + 1], sw[i]
open(w + "/test.v64", "wb").write(sw)
PY

mkdir scripts proj
cat > scripts/Dump.java <<'JAVA'
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Symbol;
public class Dump extends GhidraScript {
  @Override public void run() throws Exception {
    println("LANG=" + currentProgram.getLanguageID());
    for (MemoryBlock b : currentProgram.getMemory().getBlocks()) println("BLOCK=" + b.getName() + "@" + b.getStart());
    Address a = toAddr(0x80000400L);
    println("WORD=" + Integer.toHexString(currentProgram.getMemory().getInt(a)));
    disassemble(a);
    Instruction i = currentProgram.getListing().getInstructionAt(a);
    println("INSN=" + (i == null ? "none" : i.toString()));
    for (Symbol s : currentProgram.getSymbolTable().getSymbols("VI_STATUS")) println("SYM=" + s.getName() + "@" + s.getAddress());
  }
}
JAVA
for ext in z64 v64; do
  "$GHIDRA/support/analyzeHeadless" "$W/proj" "p_$ext" -import "test.$ext" -loader N64LoaderWVLoader \
     -scriptPath "$W/scripts" -postScript Dump.java -noanalysis -deleteProject > "log_$ext.txt" 2>&1 || { tail -40 "log_$ext.txt"; exit 1; }
  grep -q 'LANG=MIPS:BE:64:64-32addr' "log_$ext.txt"
  grep -q 'BLOCK=\.rom@' "log_$ext.txt"
  grep -q 'BLOCK=\.ram@' "log_$ext.txt"
  grep -q 'BLOCK=\.vireg@' "log_$ext.txt"
  grep -q 'WORD=27bdfff8' "log_$ext.txt"
  grep -q 'INSN=addiu sp,sp,-0x8' "log_$ext.txt"
  grep -q 'SYM=VI_STATUS@' "log_$ext.txt"
  echo "--- $ext"; grep -oE "(LANG|WORD|INSN|SYM)=.*|BLOCK=[^ ]+" "log_$ext.txt" | grep -E 'LANG|WORD|INSN|SYM|BLOCK=\.(rom|ram|vireg)@'
done
echo "PASS ghidra-n64-loader"
