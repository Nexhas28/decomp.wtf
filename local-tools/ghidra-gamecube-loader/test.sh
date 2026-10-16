#!/usr/bin/env bash
# Smoke test: install the GameCube/Wii loader extension into a throwaway Ghidra user-settings dir
# (user.home is redirected; the real Ghidra install and ~/Library/ghidra are untouched), hand-build a
# tiny DOL (a few PowerPC instructions incl. a Gekko paired-single op), import it with analyzeHeadless
# and check the loader picked the DOL type, created the memory blocks, and the Gekko language
# disassembles the code. (-loader-autoloadMaps false is required headless: with no .map next to the DOL the
# loader otherwise opens a "Load Symbols?" dialog, which throws HeadlessException.)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GHIDRA="${GHIDRA_INSTALL_DIR:-/Users/shanemcgovern/Documents/Codex/2026-10-14/i-want/work/ghidra-displaylink/ghidra_12.1.4_PUBLIC}"
export JAVA_HOME="${JAVA_HOME_21:-/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT; cd "$W"
VER="$(sed -n 's/^application.version=//p' "$GHIDRA/Ghidra/application.properties")"
EXT="$W/home/Library/ghidra/ghidra_${VER}_PUBLIC/Extensions"; mkdir -p "$EXT"
unzip -q "$HERE"/GameCubeLoader-*.zip -d "$EXT"
export JAVA_TOOL_OPTIONS="-Duser.home=$W/home"

# Build a minimal DOL: 0x100-byte header, .text0 at 0x80003100 (32 bytes), .data at 0x80004000 (32 bytes).
python3 -I - "$W/test.dol" <<'PY'
import struct, sys
code = struct.pack(">8I",
    0x38600005,  # li r3,5
    0x38630001,  # addi r3,r3,1
    0x1022182A,  # ps_add f1,f2,f3   (Gekko paired single)
    0x4E800020,  # blr
    0x60000000, 0x60000000, 0x60000000, 0x60000000)
data = bytes(range(32))
text_off, data_off = 0x100, 0x120
toff = [text_off] + [0]*6
doff = [0]*5 + [data_off] + [0]*5          # index 5 = .data
taddr = [0x80003100] + [0]*6
daddr = [0]*5 + [0x80004000] + [0]*5
tsize = [len(code)] + [0]*6
dsize = [0]*5 + [len(data)] + [0]*5
hdr = struct.pack(">7I11I7I11I7I11I3I", *toff, *doff, *taddr, *daddr, *tsize, *dsize, 0x80005000, 0x40, 0x80003100)
hdr = hdr.ljust(0x100, b"\0")
open(sys.argv[1], "wb").write(hdr + code + data)
PY

mkdir scripts proj
cat > scripts/Dump.java <<'JAVA'
import ghidra.app.script.GhidraScript;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.listing.Instruction;
public class Dump extends GhidraScript {
  @Override public void run() throws Exception {
    println("LANG=" + currentProgram.getLanguageID());
    println("FORMAT=" + currentProgram.getExecutableFormat());
    for (MemoryBlock b : currentProgram.getMemory().getBlocks()) println("BLOCK=" + b.getName() + "@" + b.getStart());
    // Disassemble the entry code explicitly (analysis is skipped).
    ghidra.program.model.address.Address a = toAddr(0x80003100L);
    disassemble(a);
    for (int i = 0; i < 4; i++) {
      Instruction ins = getInstructionAt(a);
      println("INS=" + (ins == null ? "none" : ins.toString()));
      if (ins == null) break;
      a = ins.getNext() == null ? a.add(4) : ins.getNext().getAddress();
    }
  }
}
JAVA
"$GHIDRA/support/analyzeHeadless" "$W/proj" p -import test.dol -loader-autoloadMaps false \
   -scriptPath "$W/scripts" -postScript Dump.java -noanalysis -deleteProject > log.txt 2>&1 || { tail -60 log.txt; exit 1; }
grep -E '(LANG|FORMAT|BLOCK|INS)=' log.txt | sed -E 's/.*(LANG|FORMAT|BLOCK|INS)=/\1=/' || true
chk() { grep -qi -- "$1" log.txt || { echo "FAIL: expected '$1' in analyzeHeadless output" >&2; tail -60 log.txt >&2; exit 1; }; }
chk 'LANG=PowerPC:BE:32:Gekko_Broadway'
chk 'BLOCK=MAIN_.text0@80003100'
chk 'BLOCK=MAIN_.data5@80004000'
chk 'INS=li r3,0x5'
chk 'INS=ps_add'
echo "PASS ghidra-gamecube-loader"
