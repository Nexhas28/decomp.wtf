# armips

An assembler for MIPS (N64, PSX, PS2 style code) and ARM, by Kingcom. Used to assemble small `.asm` files into exact bytes, which helps check that hand-written or decompiled assembly produces the same machine code as the original game.

Why it helps decomp.wtf: when a matching project needs an assembly snippet (for example a hand-written function or data patch) to become bytes at a fixed address, armips does that with the right endianness and load address via `.n64` / `.psx` and `.create` directives.

How to run: `local-tools/armips/bin/armips <file.asm>`. It writes the output file named inside the `.create` block next to where you run it.

Installed: built from source at Kingcom/armips commit 62adab4ef30da765f5cf22a451eb08a59c54dc8b (v0.11.0 line, MIT licence), with the ext/filesystem submodule at 3f1c185. Built natively for macOS arm64 with CMake in a scratch folder and copied only the `armips` binary into `bin/`.

Test: `bash local-tools/armips/test.sh` assembles the same three-instruction routine for N64 (big-endian) and PSX (little-endian) and checks the exact bytes. Last run: PASS.
