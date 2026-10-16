#!/usr/bin/env bash
# Smoke test: install the PSX loader into a throwaway Ghidra user-settings dir (user.home is
# redirected, the real Ghidra install and ~/Library/ghidra are untouched), import a tiny
# hand-made PS-X EXE (4 MIPS instructions, no game data) with analyzeHeadless and check the
# loader picked the PSX language, built the PSX memory map and created the "start" function.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GHIDRA="${GHIDRA_INSTALL_DIR:-/Users/shanemcgovern/Documents/Codex/2026-10-14/i-want/work/ghidra-displaylink/ghidra_12.1.4_PUBLIC}"
export JAVA_HOME="${JAVA_HOME_21:-/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT; cd "$W"
VER="$(sed -n 's/^application.version=//p' "$GHIDRA/Ghidra/application.properties")"
EXT="$W/home/Library/ghidra/ghidra_${VER}_PUBLIC/Extensions"; mkdir -p "$EXT"
unzip -q "$HERE"/ghidra_*_ghidra_psx_ldr.zip -d "$EXT"
export JAVA_TOOL_OPTIONS="-Duser.home=$W/home"

# Build a minimal PS-X EXE: 0x800-byte header + 0x800 bytes of code loaded at 0x80010000.
python3 -I - "$W/test.exe" <<'PY'
import struct, sys
code = struct.pack('<IIII', 0x3C021F80,   # lui   v0,0x1f80
                            0x2403002A,   # addiu v1,zero,0x2a
                            0xAC430000,   # sw    v1,0(v0)
                            0x03E00008)   # jr    ra
code += struct.pack('<I', 0)              # nop (delay slot)
code = code.ljust(0x800, b'\0')
hdr = bytearray(0x800)
hdr[0:8] = b'PS-X EXE'
# initial pc, gp, load addr, size, data addr/size, bss addr/size, sp base, sp offset
hdr[0x10:0x38] = struct.pack('<IIIIIIIIII', 0x80010000, 0, 0x80010000, len(code), 0, 0, 0, 0, 0x801FFF00, 0)
open(sys.argv[1], 'wb').write(bytes(hdr) + code)
PY

mkdir scripts proj
cat > scripts/Dump.java <<'JAVA'
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.mem.MemoryBlock;
public class Dump extends GhidraScript {
  @Override public void run() throws Exception {
    println("LANG=" + currentProgram.getLanguageID());
    for (MemoryBlock b : currentProgram.getMemory().getBlocks()) println("BLOCK=" + b.getName() + "@" + b.getStart());
    for (Function f : currentProgram.getFunctionManager().getFunctions(true)) println("FUNC=" + f.getName() + "@" + f.getEntryPoint());
    println("INSN0=" + currentProgram.getListing().getInstructionAt(toAddr(0x80010000L)));
  }
}
JAVA
"$GHIDRA/support/analyzeHeadless" "$W/proj" p -import test.exe -loader PsxLoader \
   -scriptPath "$W/scripts" -postScript Dump.java -noanalysis -deleteProject > log.txt 2>&1 || { tail -40 log.txt; exit 1; }
grep -q 'LANG=PSX:LE:32:default' log.txt
grep -q 'BLOCK=CODE@80010000' log.txt
grep -q 'BLOCK=CACHE@1f800000' log.txt
grep -q 'FUNC=start@80010000' log.txt
grep -q 'INSN0=lui v0,0x1f80' log.txt
grep -oE "(LANG|BLOCK|FUNC|INSN0)=[^(]*[^ (]" log.txt | sort -u
echo "PASS ghidra-psx-ldr"
