# asm-processor

Pre-processes C files and post-processes object files so hand-written MIPS assembly can sit inside IDO-compiled C (the `GLOBAL_ASM` trick used in N64, PSX and PS2 matching projects). Upstream: https://github.com/simonlindholm/asm-processor

Useful for decomp.wtf because byte-matching projects often keep some functions as assembly while the rest is compiled C, and this tool makes both land in the same `.text` section.

Run it as: `bin/asm-processor --asm-prelude prelude.inc <cc> -- <as> <as flags> -- <cc flags> -o out.o in.c`. The `prelude.inc` file here is the upstream macro prelude (`glabel`, `endlabel`, float register aliases).

Installed from the 1.0.1 tag (commit 52d6454e9d2ac8ed30fe523c0b58647d65d6199a) with `cargo install --path rust --locked`, macOS arm64, native build. The binary is copied to `bin/asm-processor`.

Test: `bash test.sh` compiles a tiny C file with a `GLOBAL_ASM` block through IDO and checks the disassembly. Last run 2026-10-16: PASS. It needs an IDO cc and a MIPS assembler from outside this folder (see the header comment in test.sh).
