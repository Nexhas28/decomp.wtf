---
type: Tool
title: pygfxd
description: Python ctypes bindings for libgfxd, which disassembles N64 F3D/F3DEX2 display lists into gsSP/gsDP C macros.
resource: https://github.com/ethteck/pygfxd
tags: [n64, disassembler]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:15:40Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:15:40Z }
sources:
  - id: repo
    resource: https://github.com/ethteck/pygfxd
    title: pygfxd README
---

# pygfxd

pygfxd wraps libgfxd, the N64 Reality Display Processor display-list disassembler, so Python can call it. It reads raw big-endian Gfx words for the F3D, F3DB, F3DEX, F3DEXB or F3DEX2 microcodes and prints each command as a `gsSP*`/`gsDP*` macro, the form N64 decomp projects use for display lists. [^repo]

Use it to turn a display list dumped from a ROM into readable C macros that can be diffed against a decomp target. The local copy is built from upstream commit fd11c99.

## Local install
Path `local-tools/pygfxd/`, commit fd11c99, entry point `run`. Runs natively on macOS arm64. `lib/libgfxd.so` was linked by hand because the upstream Makefile uses GNU-only linker flags.

## Examples
```bash
local-tools/pygfxd/run <display-list.bin> f3dex2
```

## Test result
pass, 2026-10-16. `test.sh` writes a two-command F3DEX2 display list (G_RDPPIPESYNC, G_ENDDL) to a temp file, disassembles it, and checks that the output contains `gsDPPipeSync()` and `gsSPEndDisplayList()`.

## Related
- [gfxdis](/tools/gfxdis.md) (other N64 display-list tooling in this bundle)

[^repo]: pygfxd README
