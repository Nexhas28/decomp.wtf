---
type: Tool
title: pypcode
description: Python bindings to Ghidra's SLEIGH library that disassemble raw machine code and lift it to P-code for byte-level checks.
resource: https://github.com/angr/pypcode
tags: [n64, psx, ps2, gamecube, nds, gba, disassembler]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:04:31Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:04:31Z }
sources:
  - id: repo
    resource: https://github.com/angr/pypcode
    title: pypcode README
---

# pypcode

pypcode exposes Ghidra's SLEIGH processor specifications to Python. It disassembles raw bytes and translates each instruction into P-code, Ghidra's intermediate language, which makes it possible to script checks of what a byte sequence does. [^repo] It covers the MIPS, ARM and PowerPC targets used by N64, PSX, GBA, NDS and GameCube projects.

## Local install
Path `local-tools/pypcode/`, version 4.0.1 (tag v4.0.1), installed from PyPI into `.venv/` with uv on Python 3.12. Entry point is `.venv/bin/python`, which runs natively on macOS arm64.

## Examples
```bash
.venv/bin/python -I -c 'from pypcode import Context; print([i.mnem+" "+i.body for i in Context("MIPS:BE:32:default").disassemble(bytes.fromhex("27bdffe8"), 0x80000000).instructions])'
```

## Test result
PASS, 2026-10-16. `test.sh` disassembles MIPS BE bytes (`addiu sp, sp, -0x18`, `jr ra`), checks that a MIPS `addiu` lifts to a P-code `INT_ADD`, and disassembles one ARM instruction (`bx lr`) and two PowerPC BE instructions (`stwu r1, -0x10(r1)`, `blr`).

## Related
Complements the assembler and disassembler tools in [keystone](/tools/keystone.md) and capstone for instruction-level checks, and uses the same SLEIGH specs as ghidra (see [Ghidra integration](/project/ghidra-integration.md)).

[^repo]: pypcode README
