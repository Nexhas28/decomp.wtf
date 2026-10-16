---
type: Tool
title: RetDec
description: Open-source (MIT) machine-code decompiler from avast that produces C-like code from MIPS, PowerPC, ARM and other binaries.
resource: https://github.com/avast/retdec
tags: [mips, powerpc, arm, decompiler]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:05:00Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:05:00Z }
sources:
  - id: repo
    resource: https://github.com/avast/retdec
    title: RetDec README
---

# RetDec

RetDec decompiles compiled binaries into C-like code, with support for MIPS, PowerPC and ARM among other architectures [^repo]. For byte-matching work it is useful as a fast first-pass reading of an unknown function, before the code is written by hand and checked against the original bytes. Its output is not byte-exact, so it does not replace matching tools such as [asm-differ](/tools/asm-differ.md).

## Local install

Path `local-tools/retdec/`, upstream tag v5.0 (commit 53e55b4b26e9b843787f0e06d867441e32b1604e). Entry point: `local-tools/retdec/run`, which runs the x86_64 `bin/retdec-decompiler` under Rosetta 2 (`arch -x86_64`).

## Examples

```bash
local-tools/retdec/run input.elf -o out.c
```

Smoke test core: `bash local-tools/retdec/test.sh`, which builds tiny MIPS, PowerPC and ARM ELF files and checks the recovered `add3` function.

## Test result

Pass, 2026-10-16 (exit 0). `test.sh` compiles a tiny C file for MIPS, PowerPC and ARM, decompiles each ELF, and checks that the output contains the `add3` signature and the `return add3(argc, 2, 3);` call.

## Related

- [asm-differ](/tools/asm-differ.md)
- [GhidraBoy](/tools/ghidraboy.md)

[^repo]: RetDec README (https://github.com/avast/retdec)
