---
type: Tool
title: mGBA
description: Open-source Game Boy Advance emulator with a CLI debugger, used to run and inspect small GBA ROMs for byte-matching checks.
resource: https://github.com/mgba-emu/mgba
tags: [gba, emulator, debugger]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:04:00Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:04:00Z }
sources:
  - id: repo
    resource: https://github.com/mgba-emu/mgba
    title: mgba README
---

# mGBA

mGBA is a GBA emulator that also ships a headless build and a CLI debugger [^repo]. It can run a ROM until a given instruction (for example an SWI), then report registers and disassembly. That makes it useful for checking what compiled GBA code actually does when a decompiled function is compared against the original behaviour.

## Local install

Path `local-tools/mgba/`, upstream commit 143adf5 (embedded version 0.11-1-143adf5; latest tag 0.10.5). Entry points `bin/mgba-headless` (CLI) and `bin/mgba` (GUI). Native arm64 macOS, no Rosetta or Docker.

## Examples

```bash
local-tools/mgba/bin/mgba-headless -S 3 -R r0 rom.gba
```

## Test result

PASS on 2026-10-16. `test.sh` builds a tiny GBA ROM with arm-none-eabi-as/ld, runs it headless and expects exit code 42, then drives the debugger: breakpoint hit at 0x080000D2, r0 equals 0x2A, and the disassembly at 0x080000D0 shows `mul r0, r1`.

## Related

- [asm-differ](/tools/asm-differ.md)
- [binwalk](/tools/binwalk.md)

[^repo]: mgba README
