---
type: Tool
title: Ghidra PSX Loader
description: Ghidra extension that loads PlayStation 1 PS-X EXE files with the PSX memory map, hardware registers, GP setup and PsyQ signatures.
resource: https://github.com/lab313ru/ghidra_psx_ldr
tags: [psx, ghidra-extension, loader]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:06:58Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:06:58Z }
sources:
  - id: repo
    resource: https://github.com/lab313ru/ghidra_psx_ldr
    title: ghidra_psx_ldr README
---

# Ghidra PSX Loader

A Ghidra loader for PlayStation 1 "PS-X EXE" files. It parses the EXE header, lays out RAM, scratchpad and the hardware register blocks (DMA, GPU, SPU, timers, interrupt controller), creates the `start` function, sets the GP register, and ships PsyQ library signatures, data types, GTE macro handling and a .SYM import script.[^repo] For byte-matching PSX decompilation this gives a correctly mapped Ghidra database of the original executable, which helps in identifying SDK (PsyQ) functions and hardware access before writing matching C.

## Local install
Path `local-tools/ghidra-psx-ldr/`, release 2026.10.03 (prebuilt asset for Ghidra 12.1.4), entry point `ghidra_12.1.4_PUBLIC_20261004_ghidra_psx_ldr.zip` (unzip into a Ghidra user `Extensions/` dir). Needs Java 21. Runs natively on macOS arm64.

## Examples
```bash
# inside a Ghidra user dir that has the extension unzipped into Extensions/
analyzeHeadless <proj-dir> <proj-name> -import GAME.EXE -loader PsxLoader -noanalysis
```

## Test result
Pass, 2026-10-16T12:06:58Z. `local-tools/ghidra-psx-ldr/test.sh` installs the extension into a throwaway user.home, imports a hand-made 4-instruction PS-X EXE with analyzeHeadless and checks language PSX:LE:32:default, the CODE and CACHE blocks, function `start` at 0x80010000 and the first instruction `lui v0,0x1f80`.

## Related
[PsyQ SDK and PSX compilers](/project/toolchains.md), [GBA Ghidra Loader](/tools/gba-ghidra-loader.md), [mkpsxiso](/tools/mkpsxiso.md)

[^repo]: ghidra_psx_ldr README
