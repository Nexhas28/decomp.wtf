# rz-ghidra

Rizin plugin (LGPL-3.0, from rizinorg/rz-ghidra) that brings Ghidra's decompiler into rizin as the `pdg` command, plus Ghidra's SLEIGH disassembler for many CPUs. It turns a binary's functions into C-like pseudo-code, which helps when reading game code before writing a matching decompilation.

Install: the three plugins (`core_ghidra`, `asm_ghidra`, `analysis_ghidra`) and their SLEIGH specs are built into `plugins/`, against the rizin 0.9.1 in `../rizin/`. Source commit is not recorded in this folder (see tool.json notes).

Run: `./run -q -c 'aaa; s sym.main; pdg' file.elf` (wrapper loads the three plugins into rizin).

Test: `bash test.sh` builds a tiny `add3()` function for MIPS BE, PowerPC and ARM ELF files (clang + ld.lld, arm-none-eabi-gcc), decompiles it with `pdg` and checks the output is `return ... + ... + ...;`. Last run: PASS (mips, ppc and arm all decompiled).
