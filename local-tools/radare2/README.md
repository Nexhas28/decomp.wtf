# radare2

radare2 is an open-source reverse engineering framework (disassembler, assembler, binary analyser, emulator). For decomp.wtf it is useful for disassembling MIPS (N64/PS2), Thumb (GBA/NDS), PowerPC (GameCube/Wii) and SM83 (Game Boy) code, and for running ESIL emulation to check what a short routine computes.

- Repo: https://github.com/radareorg/radare2
- Version: 6.2.4 (release tag, published 2026-10-05; tag commit 485e5e205b190fc297a2a95a6f982ff5d9f54039)
- Installed in: `local-tools/radare2/` (`bin/`, `lib/`, `share/`, `include/`), built natively for macOS arm64
- Entry point: `bin/radare2` (also `bin/rasm2`, `bin/rabin2`, `bin/rax2` and the others in `bin/`)
- Run: `local-tools/radare2/bin/rasm2 -a mips -b 32 -e -d 27bdffe8` prints `addiu sp, sp, -0x18`
- Test: `bash local-tools/radare2/test.sh` runs 9 checks (assemble/disassemble for MIPS, Thumb, PPC, SM83, ESIL emulation, and function analysis of a self-built Thumb object when `arm-none-eabi-gcc` is present)
- Test result: pass (run 2026-10-16, exit 0, all checks printed `ok:`)
- Note: the binaries link to the `lib/` folder by absolute path inside this project, so keep the folder where it is.
