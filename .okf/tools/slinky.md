---
type: Tool
title: slinky
description: Linker script generator from decompals that builds GNU ld and LLVM lld scripts for matching decompilation projects from a small YAML segment list.
resource: https://github.com/decompals/slinky
tags: [n64, linker-script-generator]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:15:00Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:15:00Z }
sources:
  - id: repo
    resource: https://github.com/decompals/slinky
    title: slinky README
---

# slinky

slinky reads a YAML file that lists each segment of a binary and the object files that belong in it, then writes a linker script for GNU `ld` or LLVM `lld` [^repo]. For matching decompilation this produces a linker layout that rebuilds the original ROM addresses, and it allows fixed VRAM addresses per segment. Its YAML input is easier to change than a hand-written linker script when files are added or moved.

## Local install
Path `local-tools/slinky/`, version 0.6.0 (tag, commit d01d5c1), entry point `bin/slinky-cli`. Built from source with `cargo install --locked` and runs natively on macOS arm64.

## Examples
```bash
local-tools/slinky/bin/slinky-cli path/to/input.yaml -o out.ld
```

## Test result
Pass, 2026-10-16. `local-tools/slinky/test.sh` writes a two-segment YAML and checks that the generated script contains the SECTIONS block, the `.header` section with `SUBALIGN(32)`, the object-path mappings for both segments, the fixed VRAM address `0x80000400`, that stdout output carries the same mapping, and that a missing input file makes the tool exit non-zero.

## Related
Related tools are listed in the bundle under [/tools/](/tools/). See [asm-differ](/tools/asm-differ.md) for the matching check that follows a link step.

[^repo]: slinky README
