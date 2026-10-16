---
type: Tool
title: ghidriff
description: Command-line binary diffing engine built on Ghidra that reports added, deleted and modified functions between two builds with decompiled-code diffs.
resource: https://github.com/clearbluejar/ghidriff
tags: [ghidra, binary-diffing, x86, mips, arm, ppc]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:16:30Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:16:30Z }
sources:
  - id: repo
    resource: https://github.com/clearbluejar/ghidriff
    title: ghidriff README
---

# ghidriff

ghidriff imports two versions of a binary into Ghidra headless (via pyghidra), analyzes both, matches functions with the Version Tracking engine and writes markdown and JSON reports listing added, deleted and modified functions with pseudocode diffs.[^repo] For byte-matching decompilation it gives a quick, GUI-free way to see which functions changed between two builds, for example a recompiled build against the original or two compiler flag sets, to guide where to keep working.

## Local install
Path `local-tools/ghidriff/`, version v1.0.0 (PyPI wheel, venv at `.venv/` with pyghidra 3.1.0). Entry point `bin/ghidriff`, a wrapper that sets JDK 21 and uses the Ghidra 12.1.4 install read-only. Runs natively on macOS arm64.

## Examples
```bash
local-tools/ghidriff/bin/ghidriff v1.elf v2.elf -o out -p proj -n t --no-symbols
# out/v1.elf-v2.elf.ghidriff.md and out/json/v1.elf-v2.elf.ghidriff.json
```

## Test result
Pass, 2026-10-16. `test.sh` builds two tiny x86-64 ELFs where only `foo()` differs, runs ghidriff and checks the JSON lists `foo` as modified and `bar` as unchanged.

## Related
[Ghidra](/project/toolchains.md), [pypcode](/tools/pypcode.md), [ghidra-delinker](/tools/ghidra-delinker.md), [asm-differ](/tools/asm-differ.md)

[^repo]: ghidriff README
