---
type: Tool
title: N64Recomp
description: Static recompiler from N64Recomp that converts N64 MIPS ELF code and RSP microcode into portable C, with CLIs N64Recomp and RSPRecomp.
resource: https://github.com/N64Recomp/N64Recomp
tags: [n64, rsp, recompiler]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:20:00Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:20:00Z }
sources:
  - id: repo
    resource: https://github.com/N64Recomp/N64Recomp
    title: N64Recomp README
---

# N64Recomp

N64Recomp is a static recompiler that translates N64 MIPS machine code into C, one function per output file, named from the ELF symbols. Its sibling RSPRecomp does the same for RSP microcode. This lets a function's behaviour be read and run as C, which helps when checking how a decompiled function compares with the original. [^repo]

## Local install
Path `local-tools/n64recomp/`, pinned to commit ffb39cdad1da5de07eaaa48bd1db4a89a7986771. Entry points `bin/N64Recomp` and `bin/RSPRecomp`. Runs natively on macOS arm64; no Rosetta or Docker.

## Examples
```bash
bin/N64Recomp n64.toml   # [input] elf_path = "game.elf", output_func_path = "out"
```

## Test result
Pass, 2026-10-16. `test.sh` builds a tiny big-endian MIPS ELF (`addiu $v0,$a0,1`) and checks that N64Recomp emits `ctx->r2 = ADD32(ctx->r4, 0X1);`. It also builds a tiny RSP blob and checks that RSPRecomp emits `r8 = RSP_ADD32(r8, 0X1);`.

## Related
Related tools: [n64sym](/tools/n64sym.md), [armips](/tools/armips.md), [asm-differ](/tools/asm-differ.md).

[^repo]: N64Recomp README
