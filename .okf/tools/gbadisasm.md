---
type: Tool
title: gbadisasm
description: Disassembler for Game Boy Advance ROMs that turns ARM and Thumb code into assembly guided by a config file.
resource: https://github.com/jiangzhengwenjz/gbadisasm
tags: [gba, disassembler, arm]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:03:26Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:03:26Z }
sources:
  - id: repo
    resource: https://github.com/jiangzhengwenjz/gbadisasm
    title: gbadisasm README
---

# gbadisasm

gbadisasm is a disassembler for Game Boy Advance ROMs. It reads a `.gba` file and prints ARM and Thumb assembly, and a config file lets you mark functions, labels, jump tables and literal pools so the output is readable and reassemblable. This is the kind of GBA assembly output that byte-matching decomp work starts from [^repo].

## Local install
Path `local-tools/gbadisasm/`, pinned to commit 940def2c91d678ee671716b8b44a2223c0ff2c62, entry point `bin/gbadisasm`. Built from source with make on macOS arm64 natively.

## Examples
```bash
local-tools/gbadisasm/bin/gbadisasm rom.gba -c rom.cfg > out.s
```

## Test result
PASS on 2026-10-16. The smoke test assembles a tiny ARM+Thumb ROM, disassembles it with a config file, and checks the output for `thumb_func_start ThumbMain`, `muls r0, r1, r0`, `svc #3` and the literal-pool `ldr`.

## Related
- [asm-differ](/tools/asm-differ.md)
- m2c (not in local-tools)

[^repo]: gbadisasm README
