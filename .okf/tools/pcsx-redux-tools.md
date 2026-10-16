---
type: Tool
title: PCSX-Redux CLI tools (psyq-obj-parser, ps1-packer)
description: Command-line PSY-Q object converter and PS-EXE packer from the PCSX-Redux PlayStation 1 emulator project.
resource: https://github.com/grumpycoders/pcsx-redux
tags: [psx, object-converter, exe-packer]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:16:41Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:16:41Z }
sources:
  - id: repo
    resource: https://github.com/grumpycoders/pcsx-redux
    title: pcsx-redux README
---

# PCSX-Redux CLI tools (psyq-obj-parser, ps1-packer)

PCSX-Redux is an open-source PlayStation 1 emulator and development suite that also ships standalone tools [^repo]. `psyq-obj-parser` reads Sony PSY-Q SDK `.OBJ` files (the "LNK" object format), dumps their sections, symbols and relocations, and converts them to standard MIPS ELF objects. For PS1 byte-matching decompilation this lets modern tools (objdiff, binutils, linkers) work with original SDK library objects. `ps1-packer` loads PS-EXE, ELF, CPE or PSF files and writes PS-EXE, CPE, raw or ROM output, which helps turn a decomp ELF build into a runnable PS-EXE.

## Local install
Path `local-tools/pcsx-redux-tools/`, commit e7ced7b681296ace22f7847f7bb3c9a206315571, entry points `bin/psyq-obj-parser` and `bin/ps1-packer`. Built natively for macOS arm64 from only the needed sources and submodules; the emulator itself is not included.

## Examples
```bash
local-tools/pcsx-redux-tools/bin/psyq-obj-parser test.obj -d
local-tools/pcsx-redux-tools/bin/psyq-obj-parser test.obj -o test.o
local-tools/pcsx-redux-tools/bin/ps1-packer in.ps-exe -o out.ps-exe
```

## Test result
pass, 2026-10-16. `test.sh` hand-builds a tiny PSY-Q LNK object (one `.text` section, exported `my_func`), dumps it and converts it to an EM_MIPS little-endian ELF (checks code bytes and symbol), then packs a hand-made PS-EXE and checks the PS-X EXE and CPE outputs.

## Related
[mkpsxiso](/tools/mkpsxiso.md), [ghidra-psx-ldr](/tools/ghidra-psx-ldr.md), [objdiff](/project/toolchains.md), [splat](/project/toolchains.md)

[^repo]: pcsx-redux README
