# ghidraboy

GhidraBoy is an open-source Ghidra extension (by Gekkio, Apache-2.0) that adds Sharp SM83 CPU support and a Game Boy ROM loader. It sets up the hardware memory map (ROM, VRAM, WRAM, I/O, HRAM) and names the 0xFF00 hardware registers, so Game Boy code disassembles and decompiles properly in Ghidra. That is the starting point for matching a Game Boy decompilation.

How to run: the extension zip is unpacked into a Ghidra user-extensions folder, then `analyzeHeadless` imports a ROM with the `GameBoyLoader`. `test.sh` does this end to end in a throwaway Ghidra user dir.

Install: upstream release 20250830 (jar GhidraBoy-1.7.1, repo Gekkio/GhidraBoy, now archived). Upstream only lists Ghidra 11.x, so this zip is a local port to Ghidra 12.1.4 using `ghidra-12.1-port.patch` in this folder. The zip's SHA-256 is recorded in `tool.json`.

Test result: pass. `test.sh` builds a 32 KB Game Boy ROM with rgbds, imports it, and checks that the SM83 language, the `rom` memory block, and the decoded instructions (`LD A,0x2a`, `LD (0xc000),A`, `HALT`) are correct.

Requires Java 21 (Homebrew `openjdk@21`) and rgbds (`rgbasm`, `rgblink`, `rgbfix`).
