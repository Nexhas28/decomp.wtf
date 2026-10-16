---
type: Tool
title: ds-rom
description: Rust CLI (dsrom) that dumps, extracts and rebuilds Nintendo DS ROMs for decomp projects.
resource: https://github.com/AetiasHax/ds-rom
tags: [nds, rom-extract-build]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:03:18Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:03:18Z }
sources:
  - id: repo
    resource: https://github.com/AetiasHax/ds-rom
    title: ds-rom README
---

# ds-rom

ds-rom is a Rust tool for Nintendo DS ROMs. The `dsrom` binary can dump a ROM's header, extract it into its ARM9 and ARM7 binaries, ITCM/DTCM autoloads, banner and header logo, and file system, then rebuild a ROM from the extracted config. This lets a DS decomp project split a ROM into editable parts and rebuild it byte-for-byte [^repo].

## Local install
Path `local-tools/ds-rom/`, version v0.8.0 (ds-rom-cli 0.8.0), entry `bin/dsrom`, runs natively on macOS arm64. Needs the sibling `local-tools/ndstool` for its smoke test.

## Examples
```bash
bash local-tools/ds-rom/test.sh
local-tools/ds-rom/bin/dsrom extract --rom game.nds --path out/
local-tools/ds-rom/bin/dsrom build --config out/config.yaml --rom rebuilt.nds
```

## Test result
PASS on 2026-10-16. The smoke test builds a synthetic DS ROM with arm-none-eabi-gcc and ndstool, then checks the header dump, extracted files, ITCM/DTCM bytes and that a second extract/build round trip is byte-identical.

## Related
- [ndstool](/tools/ndstool.md)

[^repo]: ds-rom README
