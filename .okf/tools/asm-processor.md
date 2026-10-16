---
type: Tool
title: asm-processor
description: Rust tool that lets hand-written MIPS assembly live inside IDO-compiled C for byte-matching decompilation.
resource: https://github.com/simonlindholm/asm-processor
tags: [n64, psx, ps2, compiler-wrapper]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:03:42Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:03:42Z }
sources:
  - id: repo
    resource: https://github.com/simonlindholm/asm-processor
    title: asm-processor README
---

# asm-processor

asm-processor pre-processes `.c` files and post-processes `.o` files so that `GLOBAL_ASM` blocks of MIPS assembly are emitted into the same object as IDO-compiled C functions. Matching projects use it to keep some functions as exact hand-written assembly while the rest stays C. The repo ships a Python original and a Rust rewrite; the Rust binary is what is installed here [^repo].

## Local install

Path `local-tools/asm-processor/`, version 1.0.1 (commit 52d6454e9d2ac8ed30fe523c0b58647d65d6199a), entry point `bin/asm-processor`. Built natively for macOS arm64 with cargo.

## Examples

```bash
bin/asm-processor --asm-prelude prelude.inc "$IDO_CC" -- ./mips-as -march=vr4300 -32 -EB -- -c -G 0 -non_shared -Xcpluscomm -O2 -mips2 -o out.o in.c
```

## Test result

Pass, 2026-10-16. `test.sh` compiles a C file with one `GLOBAL_ASM` function between two C functions, then checks with objdump that all three symbols are in `.text` and that the hand-written instruction bytes (`3c02dead`, `3442beef`) are intact.

## Related

- [binutils-mips toolchain](/project/toolchains.md)
- [asm-differ](/tools/asm-differ.md)

[^repo]: asm-processor README
