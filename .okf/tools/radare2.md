---
type: Tool
title: radare2
description: Open-source reverse engineering framework with disassemblers and assemblers for MIPS, Thumb, PowerPC and SM83, plus ESIL emulation and binary analysis.
resource: https://github.com/radareorg/radare2
tags: [n64, psx, ps2, gamecube, wii, nds, gba, gameboy, disassembler, assembler, emulator]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:05:00Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:05:00Z }
sources:
  - id: repo
    resource: https://github.com/radareorg/radare2
    title: radare2 README
---

# radare2

radare2 is an open-source reverse engineering framework. It includes disassemblers and assemblers for many CPUs, a binary analyser, and an ESIL emulator [^repo]. For byte-matching decompilation it helps to disassemble target code (MIPS for N64 and PS2, Thumb for GBA and NDS, PowerPC for GameCube and Wii, SM83 for Game Boy) and to check what a short routine computes by emulating it.

## Local install
Path `local-tools/radare2/`, version 6.2.4 (release tag). Entry point `bin/radare2`, with `bin/rasm2` and other tools alongside. Built natively for macOS arm64, not rosetta or docker.

## Examples
```bash
bash local-tools/radare2/test.sh
local-tools/radare2/bin/rasm2 -a mips -b 32 -e -d 27bdffe8
```

## Test result
Pass, 2026-10-16. The smoke test checks MIPS, Thumb, PowerPC and SM83 disassembly, MIPS and ARM assembly, an ESIL emulation run that must leave `v0` as `0xc`, and function analysis of a self-built Thumb object when `arm-none-eabi-gcc` is available.

## Related
- Ghidra (see [Ghidra integration](/project/ghidra-integration.md)) for a second analysis option
- [asm-differ](/tools/asm-differ.md) for comparing the disassembly of compiled output

[^repo]: radare2 README
