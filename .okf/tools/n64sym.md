---
type: Tool
title: n64sym
description: N64 symbol identification tool by shygoo that names known library functions in RAM dumps and ROMs by matching signature bytes.
resource: https://github.com/shygoo/n64sym
tags: [n64, symbol-identification]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:06:00Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:06:00Z }
sources:
  - id: repo
    resource: https://github.com/shygoo/n64sym
    title: n64sym README
---

# n64sym

n64sym scans an N64 RAM dump or ROM and reports the addresses of functions it recognises from signature files, writing them out as armips labels, splat names, Project64 or other symbol formats [^repo]. The companion n64sig builds signature files from ELF objects or libraries. This helps byte-matching decompilation by giving library code its real names before matching starts.

## Local install
Path `local-tools/n64sym/`, built from commit ccf4600 (master), entry point `bin/n64sym` with `bin/n64sig` alongside. Runs natively on macOS arm64. Built from source with macOS-specific fixes because the upstream Makefile does not link on macOS.

## Examples
```bash
local-tools/n64sym/bin/n64sym dump.bin -l f.o -f armips
```

## Test result
PASS, 2026-10-16. The smoke test assembles two MIPS functions, hides them in a fake RAM dump, and checks that n64sym reports both at the right addresses, that n64sig output works at a new offset, and that the built-in signature database loads.

## Related
Related tools: [armips](/tools/armips.md) for the label format n64sym can emit, [gclib](/tools/gclib.md) for other GameCube tooling.

[^repo]: n64sym README
