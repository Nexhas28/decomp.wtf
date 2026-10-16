---
type: Tool
title: Ghidra Delinker Extension
description: Ghidra extension that exports parts of a program as relocatable object files with a reconstructed symbol and relocation table.
resource: https://github.com/boricj/ghidra-delinker-extension
tags: [x86, mips, ghidra-extension, delinker, elf]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:06:48Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:06:48Z }
sources:
  - id: repo
    resource: https://github.com/boricj/ghidra-delinker-extension
    title: ghidra-delinker-extension README
---

# Ghidra Delinker Extension

This extension adds a "Relocation table synthesizer" analyzer and relocatable object exporters (ELF, COFF, OMF) to Ghidra. A selected piece of an analysed program (functions, sections, ranges) is exported as a .o file whose symbols and relocations are rebuilt from the Ghidra database, so a linker can reuse it directly [^repo].
For decompilation it supports Ship-of-Theseus workflows: split a binary into per-file objects, then replace pieces one by one with matching C while the linker stitches the original and rewritten parts together [^repo]. Supported targets are x86, x86-64 and MIPS (ELF), plus x86/x86-64 COFF and x86 OMF [^repo].

## Local install
Path `local-tools/ghidra-delinker/`, release v0.9.2 (commit b3890c4), prebuilt zip for Ghidra 12.1.4 kept in the folder (sha256 in tool.json). Entry point is the zip; use it via Ghidra's extension install or unzip into a user Extensions dir and run the bundled `DelinkProgram.java` headless script. Runs natively on macOS arm64 (JDK 21).

## Examples
```bash
analyzeHeadless <proj> p -import prog.elf \
  -scriptPath <ext>/ghidra-delinker-extension/ghidra_scripts \
  -postScript DelinkProgram.java /exporter "ELF relocatable object" /include foo /export out/foo.o
```

## Test result
Pass, 2026-10-16. `test.sh` builds a tiny x86-64 ELF (clang + lld), imports and delinks function `foo` headless, and checks foo.o is a relocatable x86-64 ELF with foo defined, bar/counter undefined and R_X86_64_PC32 relocations against both.

## Related
[ghidra](/project/toolchains.md), [gba-ghidra-loader](/tools/gba-ghidra-loader.md), [ghidraboy](/tools/ghidraboy.md)

[^repo]: ghidra-delinker-extension README
