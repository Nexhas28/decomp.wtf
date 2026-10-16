---
type: Tool
title: ccc (PS2 symbol dumper)
description: Parses STABS debugging symbols from PS2 .mdebug sections to list types, functions and globals.
resource: https://github.com/chaoticgd/ccc
tags: [ps2, mips, ps2-symbols, symbol-dumper]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:03:15Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:03:15Z }
sources:
  - id: repo
    resource: https://github.com/chaoticgd/ccc
    title: ccc README
---

# ccc (PS2 symbol dumper)

ccc is a library and command line set from chaoticgd that reads debugging symbols from PS2 games, mainly STABS data in `.mdebug` sections [^repo]. The `stdump` tool prints types, functions, globals and JSON from an object or ELF, which gives decomp.wtf real names and struct layouts for PS2 code.

## Local install
Path `local-tools/ccc/`, version v2.1 (latest tag at check time), entry point `bin/stdump`. Runs natively on macOS arm64. Other binaries: `bin/objdump`, `bin/demangle`, `bin/uncc`.

## Examples
```bash
bin/stdump functions file.o
```

## Test result
PASS, 2026-10-16T12:03:15Z. The smoke test (`local-tools/ccc/test.sh`) decodes an embedded tiny MIPS ELF with STABS in `.mdebug` and checks identify, types, functions, globals and JSON output, plus GNU v2 demangling (`add__3VecFi` to `Vec::add(int)`).

## Related
- [decomp-permuter](/tools/decomp-permuter.md)

[^repo]: ccc README
