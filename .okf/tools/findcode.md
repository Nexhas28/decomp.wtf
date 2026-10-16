---
type: Tool
title: findcode
description: Heuristic scanner that reports where MIPS code regions sit inside an N64 ROM (currently fails to build on macOS arm64).
resource: https://github.com/decompals/findcode
tags: [n64, rom-analysis]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:15:28Z }
sources:
  - id: repo
    resource: https://github.com/decompals/findcode
    title: findcode README
---

# findcode

findcode scans an N64 ROM and reports the address ranges that look like MIPS code, using heuristics. For byte-matching decompilation it helps decide how to split a ROM into code and data before disassembly[^repo]. The repo is stale (last push December 2022).

## Local install
Path `local-tools/findcode/`, pinned to commit 07472cd381e5e3029c33838c45e763cf76b697a6. No binary is installed: the build stops on fmt consteval errors with Apple clang 21, both with the pinned fmt and with fmt 11.0.2. Intended entry point `local-tools/findcode/bin/findcode`.

## Examples
```bash
local-tools/findcode/bin/findcode game.z64
```
This command is not runnable yet.

## Test result
Fail, 2026-10-16. `local-tools/findcode/test.sh` exits 1 because `bin/findcode` is missing. Once a binary exists, the test checks that a made-up big-endian N64 header is detected, that a region count is printed, and that a non-N64 file is rejected.

## Related
[splat](/project/toolchains.md), [n64sym](/tools/n64sym.md)

[^repo]: findcode README
