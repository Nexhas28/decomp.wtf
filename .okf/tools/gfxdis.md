---
type: Tool
title: gfxdis
description: Command-line N64 RSP display list disassembler that prints gbi.h macros for F3D, F3DEX, F3DEXB and F3DEX2 microcode.
resource: https://github.com/glankk/libgfxd
tags: [n64, disassembler]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T13:05:00Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T13:05:00Z }
sources:
  - id: repo
    resource: https://github.com/glankk/libgfxd
    title: libgfxd README
---

# gfxdis

gfxdis decodes N64 graphics display lists into readable gbi.h macro calls, so a decompiled display list can be compared against the original bytes. It supports the F3D, F3DEX, F3DEXB and F3DEX2 microcode variants, each as its own binary. The underlying decoder is the libgfxd display list library [^repo].

## Local install
Path `local-tools/gfxdis/`, binaries `bin/gfxdis.f3d`, `bin/gfxdis.f3db`, `bin/gfxdis.f3dex`, `bin/gfxdis.f3dex2`, `bin/gfxdis.f3dexb`. Version reported as gfxdis-0.7. Native macOS arm64. Source provenance is unverified because the upstream glankk/gfxdis repo returns 404.

## Examples
```bash
local-tools/gfxdis/bin/gfxdis.f3dex2 -d E700000000000000DE00000006001230DF00000000000000
```

## Test result
pass, 2026-10-16. `test.sh` checks that a hand-made F3DEX2 list (hex and file input) decodes to gsDPPipeSync(), gsSPDisplayList(0x06001230) and gsSPEndDisplayList(), and that the classic F3D binary decodes end-of-list.

## Related
- libgfxd (not in local-tools)
- [ccc](/tools/ccc.md)

[^repo]: libgfxd README
