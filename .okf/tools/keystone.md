---
type: Tool
title: Keystone
description: Multi-architecture assembler library that turns assembly text into machine-code bytes for byte-matching checks.
resource: https://github.com/keystone-engine/keystone
tags: [n64, psx, ps2, gamecube, nds, gba, assembler]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:03:52Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:03:52Z }
sources:
  - id: repo
    resource: https://github.com/keystone-engine/keystone
    title: keystone README
---

# Keystone

Keystone is an assembler library supporting ARM, MIPS, PowerPC, x86 and other architectures. It assembles text into bytes, so a rewritten function can be assembled and compared against the original bytes. [^repo] This is the reverse direction of a disassembler and complements capstone-style tools in the toolchain.

## Local install
Path `local-tools/keystone/`, version 0.9.2 (commit dc7932ef2b2c4a793836caec6ecab485005139d6), entry point `run` (wrapper around `bin/kstool`), runs natively on macOS arm64. Python binding lives in `.venv/`.

## Examples
```bash
./run mipsbe 'jr $ra'        # 03 e0 00 08
```

## Test result
PASS, 2026-10-16. `test.sh` checks MIPS big-endian (`jr $ra`), Thumb (`bx lr`), PPC32 big-endian (`blr`) through the CLI, and four ARM/MIPS/PPC/Thumb instructions through the Python binding.

## Related
Related tools: [asm-differ](/tools/asm-differ.md), [capstone and unicorn](/tools/unicorn.md).

[^repo]: keystone README
