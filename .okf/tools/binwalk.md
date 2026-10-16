---
type: Tool
title: binwalk (v3)
description: ReFirmLabs' Rust binwalk v3 scans binary dumps for embedded signatures and extracts compressed or packed data.
resource: https://github.com/ReFirmLabs/binwalk
tags: [n64, psx, gba, nds, gamecube, firmware, file-carving, decompression]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:03:01Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:03:01Z }
sources:
  - id: repo
    resource: https://github.com/ReFirmLabs/binwalk
    title: binwalk README
---

# binwalk (v3)

binwalk v3 is a Rust rewrite of the firmware and binary analysis tool. It scans a file for embedded signatures such as gzip, xz and zlib streams, filesystems and executables, and can extract them. This helps byte-matching work by locating compressed or packed data inside ROM and firmware dumps before disassembly [^repo].

## Local install

Path `local-tools/binwalk/`, version 3.1.0 (crates.io release, repository ReFirmLabs/binwalk). Entry point `bin/binwalk`. Runs natively on macOS arm64.

## Examples

```bash
local-tools/binwalk/bin/binwalk -e -C out blob.bin
```

## Test result

Pass, 2026-10-16. `test.sh` builds a hand-made blob with gzip at 0x400 and xz at 0x800 plus a standalone zlib file, runs extraction, and checks that the signatures are reported and the payloads are extracted.

## Related

- [asm-differ](/tools/asm-differ.md)
- [splat](/project/toolchains.md)

[^repo]: binwalk README (https://github.com/ReFirmLabs/binwalk)
