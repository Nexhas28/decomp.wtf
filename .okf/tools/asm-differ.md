---
type: Tool
title: asm-differ
description: Side-by-side assembly diff tool that highlights mismatching instructions between target and compiled code for byte-matching decompilation.
resource: https://github.com/simonlindholm/asm-differ
tags: [mips, n64, psx, ps2, assembler, diff]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:03:00Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:03:00Z }
sources:
  - id: repo
    resource: https://github.com/simonlindholm/asm-differ
    title: asm-differ README
---

# asm-differ

asm-differ is a Python script by simonlindholm that compares the target assembly of a function with
the assembly produced by the local compiler and marks every instruction that differs. It is the main
feedback loop for byte-matching decompilation, so a matching attempt can be checked without a full
build diff [^repo]. It supports object-file mode (`-o`), which the smoke test uses.

## Local install
Path `local-tools/asm-differ/`, commit `0dd09af8f8008f1f880327cf0aca3b26d2562ea2`, entry point `run`
(wraps `.venv/bin/asm-differ`). Runs natively on macOS arm64.

## Examples
```bash
local-tools/asm-differ/run -o -f func.o -F expected/func.o --no-pager --format plain func
```

## Test result
Pass, 2026-10-16. `test.sh` assembles a target and a mine object that differ by one `addiu` immediate
(MIPS, via binutils-mips), checks that the JSON score is above 0 with the immediate flagged, checks the
plain output shows both immediates, and checks that identical objects give score 0.

## Related
- [binutils-mips](/project/toolchains.md) (assembler used by the smoke test)

[^repo]: asm-differ README
