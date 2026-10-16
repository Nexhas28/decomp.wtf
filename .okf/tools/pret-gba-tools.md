---
type: Tool
title: pret GBA tools
description: Command-line converters from the pret GBA decompilation project (gbagfx, preproc, scaninc, gbafix and more) that rebuild GBA assets byte-for-byte.
resource: https://github.com/pret/pokeemerald
tags: [gba, asset-converter, assembler]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:05:10Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:05:10Z }
sources:
  - id: repo
    resource: https://github.com/pret/pokeemerald
    title: pokeemerald tools folder README
---

# pret GBA tools

These tools come from the tools/ folder of the pret pokeemerald project. gbagfx converts GBA graphics between PNG and 4bpp/LZ77 formats, preproc expands charmap-encoded text, scaninc lists include dependencies, and gbafix patches ROM headers. GBA decomp builds need these exact converters so that rebuilt assets match the original bytes [^repo].

## Local install
Path `local-tools/pret-gba-tools/`, built from commit 731ad5b, entry point `bin/gbagfx` (other tools in `bin/`), runs natively on macOS arm64.

## Examples
```bash
local-tools/pret-gba-tools/bin/gbagfx t.png t.4bpp
```

## Test result
PASS, 2026-10-16T12:05:10Z. The smoke test round-trips 4bpp data through PNG and LZ77, checks preproc charmap output, scaninc include scanning and gbafix header patching.

## Related
- [armips](/tools/armips.md) for assembling the code side of GBA projects.

[^repo]: pokeemerald tools README
