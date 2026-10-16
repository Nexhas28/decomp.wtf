# gba-ghidra-loader

Ghidra loader extension (Apache-2.0, https://github.com/pudii/gba-ghidra-loader) for Game Boy Advance ROMs. It parses the cartridge header, maps ROM, WRAM, IO, palette, VRAM and OAM at their GBA addresses, labels the IO registers and sets the entry point. Useful for decomp.wtf GBA projects that want to explore a ROM in Ghidra before or while matching it.

Run: `bash local-tools/gba-ghidra-loader/test.sh`. For real use, install `ghidra_12.1.4_PUBLIC_20261016_gba-ghidra-loader.zip` through Ghidra's File > Install Extensions, then import a ROM and pick "GBA Loader".

Install: repo main at commit 9bfb2d1 (release 1.1.0 plus README changes), built with Gradle against Ghidra 12.1.4. One build fix was needed, see `ghidra-12.1-build-fix.patch` (drops an unused `org.python.bouncycastle` import that no longer exists in Ghidra 12.1). The built extension zip is kept in this folder (sha256 in tool.json).

Test result: pass. The test installs the zip into a throwaway Ghidra user dir, builds a tiny GBA-shaped ROM with arm-none-eabi-as, imports it with analyzeHeadless and `-loader GBALoader`, and checks for language ARM:LE:32:v4t, the rom and VRAM blocks, and the DISPCNT label at 0x04000000. Needs Java 21.
