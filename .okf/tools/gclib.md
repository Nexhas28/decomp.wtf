---
type: Tool
title: gclib
description: Python library implementing GameCube file formats (Yaz0, RARC, GCM, DOL, REL, BTI, J3D and more).
resource: https://github.com/LagoLunatic/gclib
tags: [gamecube, library, archive, compression, file-formats]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:03:20Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:03:20Z }
sources:
  - id: repo
    resource: https://github.com/LagoLunatic/gclib
    title: gclib README
---

# gclib

gclib is a Python library for reading and writing GameCube file formats: Yaz0/Yay0 compression, RARC archives, GCM disc images, DOL executables, REL modules, BTI textures, J3D models, BMG, BFN and JPC[^repo]. For byte-matching decompilation it helps with the surrounding data work: unpacking Yaz0-compressed DOL/REL files and RARC archives, and repacking assets so a rebuilt game can be tested.

## Local install
Path `local-tools/gclib/`, pinned to commit 4e06db0004df411f1c806ad39b0e9a18ac4a1de5 (package version 1.0.0), installed into `.venv/` with uv. Entry point `local-tools/gclib/run` (runs the venv Python). Runs natively on macOS arm64. The optional native speedups were not installed.

## Examples
```bash
local-tools/gclib/run -c "from gclib.rarc import RARC; RARC('Stage.arc').extract_all_files_to_disk('out')"
```

## Test result
Pass, 2026-10-16. `local-tools/gclib/test.sh` compresses and decompresses a made-up buffer with Yaz0, then builds a RARC from scratch, saves it, re-reads it and checks the extracted file matches.

## Related
[dtk](/project/toolchains.md), [nodtool](/tools/nodtool.md), [objdiff](/project/toolchains.md)

[^repo]: gclib README
