---
type: Tool
title: rizin
description: Command-line reverse-engineering framework (radare2 fork) used to disassemble, assemble and emulate console CPU code for byte-matching work.
resource: https://github.com/rizinorg/rizin
tags: [n64, psx, ps2, gamecube, wii, gba, nds, gameboy, disassembler]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:04:52Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:04:52Z }
sources:
  - id: repo
    resource: https://github.com/rizinorg/rizin
    title: rizin README
---

# rizin

rizin is a reverse-engineering framework forked from radare2 [^repo]. For decomp.wtf it gives a second disassembler and assembler for the CPUs used in retro consoles (MIPS, Thumb, PowerPC, SM83), so instruction encodings can be cross-checked against another tool. It also has an ESIL emulator that can step through a few instructions to confirm register results.

## Local install

Path `local-tools/rizin/`, version v0.9.1, entry point `bin/rizin` (helpers in `bin/`, e.g. `bin/rz-asm`). Runs natively on macOS arm64. Libraries are in `lib/` and linked by absolute path inside the folder.

## Examples

```bash
local-tools/rizin/bin/rz-asm -a mips -b 32 -e -d 27bdffe8
# addiu sp, sp, -0x18
```

## Test result

Pass, 2026-10-16. `local-tools/rizin/test.sh` checks MIPS, Thumb, PowerPC and SM83 disassembly, MIPS and ARM assembly, an ESIL run of two MIPS adds (expects `v0 = 0x0000000c`), and `aaa; pdf` on a tiny Thumb object built with arm-none-eabi-gcc (skipped if that compiler is absent).

## Related

- [radare2](/tools/radare2.md) (same lineage, larger ecosystem)
- [retdec](/tools/retdec.md)
- [ghidraboy](/tools/ghidraboy.md)

[^repo]: rizin README
