---
type: Tool
title: rz-ghidra
description: Rizin plugin that adds Ghidra's decompiler (pdg command) and SLEIGH disassembly to rizin for many CPU architectures.
resource: https://github.com/rizinorg/rz-ghidra
tags: [n64, psx, ps2, gamecube, nds, gba, ghidra-extension, decompiler]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:06:00Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:06:00Z }
sources:
  - id: repo
    resource: https://github.com/rizinorg/rz-ghidra
    title: rz-ghidra README
---

# rz-ghidra

rz-ghidra is a plugin for rizin that runs Ghidra's decompiler inside rizin, so a binary's functions can be shown as C-like pseudo-code with the `pdg` command. It also adds Ghidra's SLEIGH disassembler for many CPUs, which covers the MIPS, PowerPC and ARM chips used by consoles such as N64, PS1/PS2, GameCube, DS and GBA.[^repo] This gives a decompilation project a quick second view of a function's logic next to the assembly it is matching.

## Local install
Path `local-tools/rz-ghidra/`, plugins built against rizin 0.9.1 in `local-tools/rizin/`. Entry point is `run`, a wrapper that loads the three plugins into rizin. Runs natively on macOS arm64. The exact source commit was not recorded, so the version is unverified (upstream latest release v0.9.0).

## Examples
```bash
local-tools/rz-ghidra/run -q -c 'aaa; s sym.main; pdg' file.elf
```

## Test result
Pass, 2026-10-16. `test.sh` builds a tiny `add3()` function as MIPS BE, PowerPC and ARM ELF files, decompiles each with `pdg` and checks the output is a sum of three arguments. Output tail: `ok: mips -> return arg2 + arg1 + arg3;`, `ok: ppc -> return arg2 + arg1 + arg3;`, `ok: arm -> return arg1 + arg2 + in_r2;`, `PASS`.

## Related
[rizin](/tools/rizin.md), [radare2](/tools/radare2.md), [Ghidra integration](/project/ghidra-integration.md), [GhidraBoy](/tools/ghidraboy.md)

[^repo]: rz-ghidra README
