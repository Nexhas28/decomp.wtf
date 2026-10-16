---
type: Tool
title: armips
description: MIPS and ARM assembler by Kingcom that turns .asm files into exact target bytes for N64 and PSX code.
resource: https://github.com/Kingcom/armips
tags: [n64, psx, assembler]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:05:00Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:05:00Z }
sources:
  - id: repo
    resource: https://github.com/Kingcom/armips
    title: armips README
---

# armips

armips is an assembler for MIPS and ARM targets. Its `.n64`, `.psx` and `.create` directives let you place assembled code at a fixed load address with the right byte order, which is what byte-matching work needs for small hand-written or decompiled routines [^repo].

## Local install
Path `local-tools/armips/`, built from commit 62adab4 (v0.11.0 line), entry point `bin/armips`, runs natively on macOS arm64.

## Examples
```bash
local-tools/armips/bin/armips n64.asm
```

## Test result
PASS, 2026-10-16. The smoke test assembles one MIPS routine for N64 (big-endian) and PSX (little-endian) and checks the exact output bytes.

## Related
Related tools: [asm-differ](/tools/asm-differ.md) for comparing assembled output against a target.

[^repo]: armips README
