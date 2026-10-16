---
type: Tool
title: Ghidra Emotion Engine Reloaded
description: Ghidra extension that adds PlayStation 2 Emotion Engine (MIPS-R5900) disassembly, decompilation and PS2-specific analyzers.
resource: https://github.com/chaoticgd/ghidra-emotionengine-reloaded
tags: [ps2, ghidra-extension, processor-module]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:05:44Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:05:44Z }
sources:
  - id: repo
    resource: https://github.com/chaoticgd/ghidra-emotionengine-reloaded
    title: ghidra-emotionengine-reloaded README
---

# Ghidra Emotion Engine Reloaded

An extension for Ghidra that adds support for the PlayStation 2: it disassembles and decompiles EE-specific instruction sets (MMI, VU0 macro mode, etc), recovers types, functions and globals from ELF files with `.mdebug` sections via its STABS Analyzer, imports PCSX2 save states and fixes global references with the MIPS-R5900 Constant Reference Analyzer.[^repo] For a PS2 matching decompilation this gives correct listings and pseudo-C for EE code that stock Ghidra MIPS mis-decodes, which is the starting point before matching with ee-gcc or mwcc-ps2.

## Local install
Path `local-tools/ghidra-emotionengine/`, release v2.1.38 (commit fcc5deac45d8f5c27c632bfe0db928630939676e), entry point is the unmodified release zip `ghidra_12.1.4_PUBLIC_20261004_ghidra-emotionengine-reloaded.zip` for Ghidra 12.1.4. Runs natively on macOS arm64 with Java 21 (the bundled `stdump` helpers are x86_64 only).

## Examples
```bash
analyzeHeadless "$W/proj" p -import ee.bin -loader BinaryLoader \
   -processor r5900:LE:32:default -scriptPath "$W/scripts" -postScript Dis.java -noanalysis -deleteProject
```

## Test result
pass, 2026-10-16. `test.sh` installs the extension into a throwaway Ghidra user.home, imports a hand-made 20-byte R5900 blob and checks that `lq a0,0x10(sp)`, `paddw v0,a0,a1` and `sq v0,0x20(sp)` disassemble under language `r5900:LE:32:default`.

## Related
[Ghidra](/project/toolchains.md), [GBA Ghidra Loader](/tools/gba-ghidra-loader.md), [pypcode](/tools/pypcode.md)

[^repo]: ghidra-emotionengine-reloaded README
