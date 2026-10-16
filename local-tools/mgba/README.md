# mgba

mGBA is an open-source Game Boy Advance emulator (https://github.com/mgba-emu/mgba) with a built-in CLI debugger. For decomp.wtf it lets you run a GBA ROM headlessly, stop at breakpoints, and read registers and disassembly to check how compiled code behaves.

Run the headless emulator: `local-tools/mgba/bin/mgba-headless -S 3 -R r0 game.gba` (exit code is r0 when it stops).
Run the GUI: `local-tools/mgba/bin/mgba game.gba`.
Debugger mode: `local-tools/mgba/bin/mgba-headless -d game.gba`, then type commands such as `break 0x08000000`, `continue`, `disassemble <addr> <n>`.

Install: prebuilt arm64 macOS binaries in `bin/`, built from upstream commit 143adf5 (embedded version string 0.11-1-143adf5; latest tag is 0.10.5). No system-wide install.

Test: `bash local-tools/mgba/test.sh` builds a tiny GBA ROM with Homebrew arm-none-eabi tools, runs it headless and checks exit code 42, then checks breakpoint, r0 and a `mul` disassembly line in the debugger. Last result: PASS.
