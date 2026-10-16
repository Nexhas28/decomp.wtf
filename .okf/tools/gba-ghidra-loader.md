---
type: Tool
title: GBA Ghidra Loader
description: Ghidra extension that loads Game Boy Advance ROMs with the correct memory map, IO labels and entry point.
resource: https://github.com/pudii/gba-ghidra-loader
tags: [gba, ghidra-extension, loader]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:03:25Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:03:25Z }
sources:
  - id: repo
    resource: https://github.com/pudii/gba-ghidra-loader
    title: gba-ghidra-loader README
---

# GBA Ghidra Loader

A Ghidra loader for GBA ROM files. It maps the GBA memory areas and IO registers at their hardware addresses, parses the cartridge header and sets the entry point.[^repo] This gives a decompilation project a correctly laid out Ghidra database for a GBA ROM, which helps when working out function boundaries and hardware register use before matching code.

## Local install
Path `local-tools/gba-ghidra-loader/`, repo commit 9bfb2d1 (release 1.1.0 plus README changes) built against Ghidra 12.1.4 with one small patch (`ghidra-12.1-build-fix.patch`). Entry point is the extension zip `ghidra_12.1.4_PUBLIC_20261016_gba-ghidra-loader.zip`, installed through File > Install Extensions or dropped into a Ghidra user Extensions folder. Runs natively on macOS arm64 and needs Java 21.

## Examples
```bash
analyzeHeadless "$W/proj" p -import rom.gba -loader GBALoader -noanalysis -deleteProject
```

## Test result
Pass, 2026-10-16. `test.sh` installs the zip into a throwaway Ghidra user dir, builds a tiny GBA-shaped ROM with arm-none-eabi-as, imports it with the GBALoader and checks the language (ARM:LE:32:v4t), the rom and VRAM blocks and the DISPCNT label at 0x04000000.

## Related
[Ghidra integration](/project/ghidra-integration.md), [GhidraBoy](/tools/ghidraboy.md), [gbadisasm](/tools/gbadisasm.md)

[^repo]: gba-ghidra-loader README
