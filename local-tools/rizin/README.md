# rizin

Command-line reverse-engineering framework (fork of radare2, LGPL-3.0). Used here for disassembling and assembling the CPUs found in retro consoles, emulating a few instructions with ESIL, and analysing functions in small object files.

Why it helps decomp.wtf: a second, independent disassembler/assembler to cross-check instruction encodings for MIPS (N64/PS2), Thumb (GBA/NDS), PowerPC (GameCube/Wii) and SM83 (Game Boy) against radare2.

How to run: `local-tools/rizin/bin/rizin` (analysis shell) or the helpers `bin/rz-asm -a mips -b 32 -e -d 27bdffe8`. Libraries are in `lib/`, linked with paths inside this folder.

Install: built locally for macOS arm64, version 0.9.1 (tag v0.9.1, GitHub tag commit c3a90e9226d977f58f4e9c75f78fa6b07afe13c7). The source clone used for the build was not kept, so the build commit is not independently confirmed.

Test: `bash test.sh` runs 8 checks (MIPS/Thumb/PPC/SM83 disassembly, MIPS/ARM assembly, ESIL emulation of two MIPS adds, and function analysis of a tiny Thumb object built with arm-none-eabi-gcc). Last run: PASS.
