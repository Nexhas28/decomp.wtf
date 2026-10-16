---
type: Tool
title: N64LoaderWV (Ghidra N64 ROM loader)
description: Ghidra loader extension that imports Nintendo 64 ROMs with the N64 memory map, hardware register labels and automatic byte-order fixing.
resource: https://github.com/zeroKilo/N64LoaderWV
tags: [n64, ghidra-extension, loader]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:07:23Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:07:23Z }
sources:
  - id: repo
    resource: https://github.com/zeroKilo/N64LoaderWV
    title: N64LoaderWV README
---

# N64LoaderWV (Ghidra N64 ROM loader)

A Ghidra loader for N64 ROMs (.z64, .n64, .v64). It normalises the byte order, maps ROM, RDRAM, boot code, PIF and the hardware register ranges at their N64 addresses and labels the registers, so a ROM can be disassembled and decompiled straight after import [^repo]. It can also take an RDRAM dump, an N64sym file or a byte-pattern file to seed function names, which helps when exploring overlay code before matching it in a decomp project [^repo].

## Local install
Path `local-tools/ghidra-n64-loader/`, upstream tag 12.1.3 (commit 2bd5376), rebuilt unmodified against Ghidra 12.1.4 with Gradle 8.11.1 and Java 21. Entry point is the extension zip `ghidra_12.1.4_PUBLIC_20261016_N64LoaderWV.zip` (install via Ghidra's Install Extensions). Runs natively on macOS arm64 (Java). The upstream repo has no license file.

## Examples
```bash
analyzeHeadless <projdir> p -import test.z64 -loader N64LoaderWVLoader -noanalysis -deleteProject
```

## Test result
Pass, 2026-10-16. `test.sh` installs the zip into a throwaway Ghidra user dir, imports a hand-built ROM-shaped .z64 and its byte-swapped .v64 with analyzeHeadless, and checks language MIPS:BE:64:64-32addr, the .rom/.ram/.vireg blocks, the VI_STATUS label and that code at 0x80000400 disassembles to `addiu sp,sp,-0x8` for both byte orders.

## Related
[GBA Ghidra loader](/tools/gba-ghidra-loader.md), [n64sym](/tools/n64sym.md), [splat](/project/toolchains.md)

[^repo]: N64LoaderWV README
