# ghidra-psx-ldr

Ghidra extension (lab313ru/ghidra_psx_ldr) that loads PlayStation 1 "PS-X EXE" files: PSX memory map and hardware register blocks, `start`/`main` detection, GP setup, GTE macro support, PsyQ signature/data-type libraries and a PSX .SYM import script.
Useful for decomp.wtf when a PSX game needs analysis in Ghidra before/while writing matching C (MIPS R3000, PsyQ SDK).

Run: unzip `ghidra_12.1.4_PUBLIC_20261004_ghidra_psx_ldr.zip` into `<ghidra user dir>/Extensions/` (or install it via Ghidra's File > Install Extensions), then import a PS-X EXE; with analyzeHeadless use `-loader PsxLoader`.
`bash local-tools/ghidra-psx-ldr/test.sh` does this in a throwaway user.home, so the real Ghidra install is untouched.

Installed from: prebuilt release asset `ghidra_12.1.4_PUBLIC_20261004_ghidra_psx_ldr.zip` of tag 2026.10.03 (https://github.com/lab313ru/ghidra_psx_ldr/releases/tag/2026.10.03), sha256 32abacd90a5950b97a0756f67e991c3cc0f8a62edd5531890d91af68a9916255. Needs Java 21 and Ghidra 12.1.4.
Test result: pass (2026-10-16). A hand-made 4-instruction PS-X EXE imports with language PSX:LE:32:default, blocks CODE/CACHE/hardware-register blocks, function `start`@80010000 and first instruction `lui v0,0x1f80`.
