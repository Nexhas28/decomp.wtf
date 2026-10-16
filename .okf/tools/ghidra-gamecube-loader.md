---
type: Tool
title: Ghidra GameCube/Wii Loader
description: Ghidra extension that loads GameCube and Wii DOL, REL, RSO, apploader and RAM-dump files with the Gekko/Broadway PowerPC language.
resource: https://github.com/Cuyler36/Ghidra-GameCube-Loader
tags: [gamecube, wii, ghidra-extension, loader]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:08:00Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:08:00Z }
sources:
  - id: repo
    resource: https://github.com/Cuyler36/Ghidra-GameCube-Loader
    title: Ghidra-GameCube-Loader README
---

# Ghidra GameCube/Wii Loader

A Ghidra loader and processor-language extension for GameCube and Wii binaries. It recognises DOL executables, REL and RSO modules, apploaders and RAM dumps, maps their sections and the console hardware memory areas, and can apply CodeWarrior .map symbol files.[^repo] For byte-matching decompilation this gives a correctly laid out Ghidra database, with Gekko paired-single instructions decoded, which helps when identifying functions and data before matching them against compiler output.

## Local install
Path `local-tools/ghidra-gamecube-loader/`, release 1.3.1 (commit 921504c, prebuilt for Ghidra 12.1) as `GameCubeLoader-1.3.0-921504c-Ghidra_12.1.zip`. Runs natively on macOS arm64 inside Ghidra 12.1.4 with Java 21. Install through Ghidra's File > Install Extensions; the test installs it into a throwaway user-settings dir instead.

## Examples
```bash
analyzeHeadless proj p -import game.dol -loader-autoloadMaps false -noanalysis
```
Headless imports need `-loader-autoloadMaps false` when no .map file sits next to the DOL, otherwise the loader opens a dialog and fails with HeadlessException.

## Test result
Pass, 2026-10-16. `local-tools/ghidra-gamecube-loader/test.sh` hand-builds a tiny DOL, imports it with analyzeHeadless and checks the Gekko_Broadway language, the `.text0` and `.data5` memory blocks, and that `li`, `addi` and `ps_add` disassemble.

## Related
[gba-ghidra-loader](/tools/gba-ghidra-loader.md), [dtk](/project/toolchains.md), [objdiff](/project/toolchains.md)

[^repo]: Ghidra-GameCube-Loader README
