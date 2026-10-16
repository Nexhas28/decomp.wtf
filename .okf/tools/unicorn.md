---
type: Tool
title: Unicorn
description: Multi-architecture CPU emulator framework (QEMU-based) with Python bindings, used to run small code or data routines and inspect their register and memory results.
resource: https://github.com/unicorn-engine/unicorn
tags: [mips, arm, ppc, emulator, python]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:04:53Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:04:53Z }
sources:
  - id: repo
    resource: https://github.com/unicorn-engine/unicorn
    title: unicorn README
---

# Unicorn

Unicorn is a CPU emulator framework built on QEMU with Python bindings. It can execute a short routine for a given architecture (MIPS, ARM, PPC and others) and report the resulting registers and memory, so a hand-written or decompiled function can be checked against the original bytes without a console [^repo].

## Local install
Path `local-tools/unicorn/`, Python package unicorn 2.1.4 in `.venv/` (entry `.venv/bin/python`). Runs natively on macOS arm64.

## Examples
```bash
local-tools/unicorn/.venv/bin/python -I -c "from unicorn import *; from unicorn.mips_const import *; mu=Uc(UC_ARCH_MIPS, UC_MODE_MIPS32+UC_MODE_BIG_ENDIAN); mu.mem_map(0x10000,0x1000); mu.mem_write(0x10000, bytes.fromhex('2402000524420007')); mu.emu_start(0x10000, 0x10008); print(mu.reg_read(UC_MIPS_REG_V0))"
```

## Test result
pass, 2026-10-16T12:04:53Z. `test.sh` emulates three tiny snippets (MIPS32 BE `addiu` chain expecting v0=12, ARM Thumb `adds` chain expecting r0=6, PPC32 BE `li`/`addi` expecting r3=42) and asserts the result registers.

## Related
- [asm-differ](/tools/asm-differ.md)
- [decomp-permuter](/tools/decomp-permuter.md)

[^repo]: unicorn README
