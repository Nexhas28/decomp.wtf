---
type: Tool
title: mapfile-parser
description: Python and Rust library plus CLI from Decompollaborate that parses GNU ld and clang lld linker maps into sections and symbols for decompilation progress tracking.
resource: https://github.com/ethteck/mapfile_parser
tags: [n64, psx, ps2, gamecube, nds, gba, linker-map-parser]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:15:42Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:15:42Z }
sources:
  - id: repo
    resource: https://github.com/ethteck/mapfile_parser
    title: mapfile_parser README
---

# mapfile-parser

mapfile-parser reads the linker map file produced when a decompilation project links its object files. It exposes the sections and symbols with their addresses and sizes through a Python API and a CLI, with utilities such as `jsonify`, `progress` and `objdiff_report` built on top [^repo]. Byte-matching projects use the parsed map to measure which functions are matched and where each symbol sits in the final ROM or executable.

## Local install
Path `local-tools/mapfile-parser/`, version 2.9.0 (commit dd01f8c1cb3250c715d0b7f4f93076531c28dc00). Entry point `.venv/bin/python -m mapfile_parser`. Runs natively on macOS arm64; the Rust core is compiled locally into the venv.

## Examples
```bash
local-tools/mapfile-parser/.venv/bin/python -m mapfile_parser jsonify path/to/game.map -o out.json
```

## Test result
Pass, 2026-10-16. `local-tools/mapfile-parser/test.sh` writes a tiny GNU ld map, checks that the Python API `MapFile.readMapFile` finds the expected function and data symbols, and checks that the `jsonify` CLI writes JSON containing them.

## Related
- [objdiff](/project/toolchains.md) for the matching diff this progress feeds into
- [splat](/project/toolchains.md) for the splitter whose build produces the map

[^repo]: mapfile_parser README
