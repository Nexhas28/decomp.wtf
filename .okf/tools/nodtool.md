---
type: Tool
title: nodtool
description: Command-line tool that reads, extracts, converts (ISO and RVZ) and verifies GameCube and Wii disc images.
resource: https://github.com/encounter/nod
tags: [gamecube, wii, disc-image-extract-convert]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:04:13Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:04:13Z }
sources:
  - id: repo
    resource: https://github.com/encounter/nod
    title: nod README
---

# nodtool

nodtool is the `nodtool` binary from the encounter/nod project. It reads GameCube and Wii disc images, shows their header info, extracts the file system (including `sys/main.dol`), converts images to ISO and to RVZ, and verifies them [^repo]. A GameCube or Wii decomp project needs exactly this step to get its `main.dol` and asset files out of a disc before splitting and matching them.

## Local install
Path `local-tools/nodtool/`, version v2.0.0-alpha.12 (commit ebd80cac99b48a323200d84365e07a58cc27412d), entry `bin/nodtool`, runs natively on macOS arm64.

## Examples
```bash
bash local-tools/nodtool/test.sh
local-tools/nodtool/bin/nodtool info game.iso
local-tools/nodtool/bin/nodtool extract -q game.iso out/
local-tools/nodtool/bin/nodtool convert game.iso game.rvz
```

## Test result
PASS on 2026-10-16. The smoke test hand-builds a tiny GCM disc image, then checks the game ID and title from `info`, the extracted file contents and `main.dol` bytes from `extract`, and that converting to RVZ and back gives a byte-identical ISO.

## Related
- No related tool notes yet.

[^repo]: nod README
