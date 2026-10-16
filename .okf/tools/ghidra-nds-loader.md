---
type: Tool
title: NTRGhidra (Nintendo DS loader for Ghidra)
description: Ghidra loader and plugin that imports Nintendo DS and DSi ROMs with the correct ARM9/ARM7 memory map, IO labels, overlays and entry point.
resource: https://github.com/pedro-javierf/NTRGhidra
tags: [nds, ghidra-extension, loader]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:09:04Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:09:04Z }
sources:
  - id: repo
    resource: https://github.com/pedro-javierf/NTRGhidra
    title: NTRGhidra README
---

# NTRGhidra (Nintendo DS loader for Ghidra)

NTRGhidra is a Ghidra loader that parses the NDS cartridge header, maps the ARM9 (ARM946E-S, `ARM:LE:32:v5t`) or ARM7 (`ARM:LE:32:v4t`) binary at its RAM address, optionally decompresses commercial SDK-compressed ARM9 code, creates the DS memory regions, labels the hardware IO registers, loads overlays and sets the entry point [^repo]. For byte-matching decompilation this gives a ready-made, correctly mapped Ghidra database of a game's main binary to compare against, with the right addresses for overlays and registers. A bundled Function ID database (`nitrosdk3.0.fidb`) helps recognise Nintendo SDK functions [^repo].

## Local install
Path `local-tools/ghidra-nds-loader/`, upstream v1.5.1 (commit 9e0674c), built against Ghidra 12.1.4 (upstream only documents 12.0.4). Entry point is the extension zip `ghidra_12.1.4_PUBLIC_20261016_NTRGhidra.zip`, installed via Ghidra's File > Install Extensions. Runs natively on macOS arm64 with Java 21. A small local patch (`ntrghidra-headless-prompts.patch`) makes the loader usable under `analyzeHeadless` by skipping its GUI dialogs there; GUI behaviour is unchanged.

## Examples
```bash
analyzeHeadless <projdir> p -import game.nds -loader NTRGhidraLoader -noanalysis
# optional: JAVA_TOOL_OPTIONS="-Dntrghidra.arm7=true -Dntrghidra.sdk=true"
```

## Test result
Pass, 2026-10-16. `test.sh` builds a tiny NDS-shaped image (valid header CRC, ARM9 stub), imports it headlessly and checks language v5t, the ARM9_Main_Memory block, IO and VRAM regions, the REG_A_DISPCNT label, the `_entry_arm9` function and the first instruction. ARM7, SDK decompression and overlay loading were not exercised.

## Related
[ndstool](/tools/ndstool.md), [ds-rom](/tools/ds-rom.md), [ndspy](/tools/ndspy.md), [gba-ghidra-loader](/tools/gba-ghidra-loader.md), [dsd](/project/toolchains.md)

[^repo]: NTRGhidra README
