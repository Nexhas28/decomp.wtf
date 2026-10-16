---
type: Tool
title: Torch
description: Generic N64 asset processor that extracts ROM assets into an OTR archive or C source.
resource: https://github.com/HarbourMasters/Torch
tags: [n64, asset-extractor]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:18:41Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:18:41Z }
sources:
  - id: repo
    resource: https://github.com/HarbourMasters/Torch
    title: Torch README
---

# Torch

Torch turns the segmented assets of an N64 ROM (textures, display lists, vertices, matrices, audio) into an OTR archive or C source [^repo]. A decomp project can compare what it extracted against Torch's output to confirm asset layouts before it matches code. It is built on libgfxd for display list decoding and StormLib for the OTR container.

## Local install

Path `local-tools/torch/`, version v1.0.0 (commit bb39229), entry point `bin/torch`. Native macOS arm64 build; no Rosetta or Docker needed.

## Examples

```bash
local-tools/torch/bin/torch pack <folder> <out.otr>
local-tools/torch/bin/torch binary <baserom.z64>   # needs config.yml in cwd keyed by ROM hash
```

## Test result

Pass, 2026-10-16T12:18:41Z. The smoke test packs a two-file folder into an MPQ-format OTR, then runs binary mode on a self-made 64 KB N64-shaped file with a config keyed by its hash, and checks that fake.otr is written.

## Related

- [splat](/project/toolchains.md)
- [asm-differ](/tools/asm-differ.md)

[^repo]: HarbourMasters/Torch README
