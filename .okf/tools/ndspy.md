---
type: Tool
title: ndspy
description: Python library for reading, editing and creating Nintendo DS ROMs, NitroFS, LZ10 compression, NARC archives and ARM code sections.
resource: https://github.com/RoadrunnerWMC/ndspy
tags: [nds, python-library, rom-extract-build]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:05:00Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:05:00Z }
sources:
  - id: repo
    resource: https://github.com/RoadrunnerWMC/ndspy
    title: ndspy README
---

# ndspy

ndspy is a Python library for Nintendo DS file formats. It opens and saves whole `.nds` ROMs, reads and writes the NitroFS file system by name, and handles LZ10/LZ11 compression and NARC archives [^repo]. A DS decomp script can use it to unpack a ROM, patch a single file or code section, and repack it without a compiled tool.

## Local install
Path `local-tools/ndspy/`, version 4.2.0 (matches upstream tag v4.2.0), Python 3.12 virtualenv at `.venv/`. Entry point `local-tools/ndspy/run`, which runs the venv Python. Runs natively on macOS arm64.

## Examples
```bash
bash local-tools/ndspy/test.sh
local-tools/ndspy/run -I -c 'import ndspy.rom; r = ndspy.rom.NintendoDSRom.fromFile("game.nds"); print(r.name)'
```

## Test result
PASS on 2026-10-16. The smoke test builds a synthetic ROM with an ARM9 blob and a NitroFS file, saves and reloads it, patches the file and checks the change, round-trips LZ10 compression and a NARC archive.

## Related
- [ds-rom](/tools/ds-rom.md)
- [ndstool](/tools/ndstool.md)

[^repo]: ndspy README
