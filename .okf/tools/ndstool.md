---
type: Tool
title: ndstool
description: Nintendo DS ROM packer and unpacker from devkitPro that builds, inspects and extracts .nds ROMs.
resource: https://github.com/devkitPro/ndstool
tags: [nds, rom-packer]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:06:00Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:06:00Z }
sources:
  - id: repo
    resource: https://github.com/devkitPro/ndstool
    title: ndstool README
---

# ndstool

ndstool is the standard DS ROM tool from devkitPro. It packs ARM9 and ARM7 binaries, NitroFS data and banners into a `.nds` file and unpacks them again, so you can confirm the header entry addresses and file layout of a rebuilt DS ROM against the original [^repo].

## Local install
Path `local-tools/ndstool/`, release 2.3.1 (commit 76e8b68), entry point `bin/ndstool`, built natively for macOS arm64.

## Examples
```bash
local-tools/ndstool/bin/ndstool -c t.nds -9 a9.bin -7 a7.bin -r9 0x02000000 -e9 0x02000000 -r7 0x02380000 -e7 0x02380000 -d fs
local-tools/ndstool/bin/ndstool -i t.nds
```

## Test result
PASS, 2026-10-16. The smoke test (`local-tools/ndstool/test.sh`) builds tiny ARM9/ARM7 programs with arm-none-eabi-gcc, packs them with a NitroFS file, checks the header info, extracts again and compares the bytes.

## Related
- [ds-rom](/tools/ds-rom.md)
- [ndspy](/tools/ndspy.md)

[^repo]: ndstool README
