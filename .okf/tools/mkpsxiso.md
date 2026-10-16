---
type: Tool
title: mkpsxiso (dumpsxiso)
description: Builds PlayStation 1 BIN/CUE disc images from an XML project and extracts disc images back to files.
resource: https://github.com/Lameguy64/mkpsxiso
tags: [psx, disc-image, iso-builder]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:04:12Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:04:12Z }
sources:
  - id: repo
    resource: https://github.com/Lameguy64/mkpsxiso
    title: mkpsxiso README
---

# mkpsxiso (dumpsxiso)

mkpsxiso builds PlayStation 1 disc images from an XML project description, and dumpsxiso extracts a disc image into its files and an XML layout[^repo]. For byte-matching decompilation it covers both ends of the disc workflow: unpacking an original PS1 disc to get the assets and file layout, and rebuilding a disc around a recompiled executable so the result can be tested.

## Local install
Path `local-tools/mkpsxiso/`, version v2.30 (commit 54fb1644ed8741223583e2dcda358b75a205e214), built with CMake from the upstream tag. Entry points `local-tools/mkpsxiso/bin/mkpsxiso` and `local-tools/mkpsxiso/bin/dumpsxiso`. Runs natively on macOS arm64.

## Examples
```bash
local-tools/mkpsxiso/bin/mkpsxiso -q -y disc.xml
local-tools/mkpsxiso/bin/dumpsxiso -q -x out -s out.xml test.bin
```

## Test result
Pass, 2026-10-16. `local-tools/mkpsxiso/test.sh` writes a small XML disc project with a root file and a subdirectory file, builds a BIN image with mkpsxiso, extracts it with dumpsxiso and checks both files match the originals byte for byte.

## Related
- [asm-differ](/tools/asm-differ.md)
- [gclib](/tools/gclib.md)

[^repo]: mkpsxiso README
