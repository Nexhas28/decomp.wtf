---
type: Pipeline
title: ROM import pipeline
description: How a game file the user owns becomes a project of target functions, from detection through splitting to the database rows.
tags: [import, pipeline, splat, rom, detection]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: engine
    resource: src/decompnet/app/engine.py
    title: RomNetwork.import_rom and platform importers
  - id: detect
    resource: src/decompnet/rom/detect.py
    title: identify()
  - id: split
    resource: src/decompnet/rom/split.py
    title: split_rom()
  - id: symbols
    resource: src/decompnet/rom/symbols.py
    title: Symbol table
  - id: chd
    resource: src/decompnet/rom/chd.py
    title: CHD reader
  - id: forkengine
    resource: src/decompnet/app/forkengine.py
    title: create_scratch()
  - id: chat
    resource: src/decompnet/app/chat.py
    title: import_game MCP tool
  - id: seeds
    resource: src/decompnet/seeds.py
    title: m2c drafts
---

# ROM import pipeline

The entry point is `RomNetwork.import_rom(data, toolchain=None, flags=None, confirmed=False, name="")`[^engine]. The app's `/api/import` endpoint and the `import_game(path, own_copy, compiler)` MCP tool both call it[^chat].

## Steps

1. **Ownership gate.** The import refuses without `confirmed=True`, with the message "confirm that this is your own legally obtained copy of the game"[^engine].
2. **Community layouts first.** `_community_layout` checks for an NDS header, a GameCube/Wii disc or a DOL. If one is found, the game goes to `forks.create_scratch`, which generates a dtk-template or ds-decomp project. The input bytes are saved under `.decomp/uploads/` first[^engine][^forkengine].
3. **Detect.** `rom.detect.identify` recognises the following, and otherwise raises `RomError` with a list of accepted formats[^detect]:
   - N64, GBA and Game Boy/Color ROMs
   - PS-X EXE and PS2 ELF programs
   - PlayStation disc images (ISO 9660, raw `.bin`, and CHD v5, which `rom/chd.py` streams hunk by hunk)[^chd]
4. **Dedup and slug.** A ROM whose SHA-1 is already in a project returns that project. Otherwise a slug is made from the title, and the file is written to `<workspace>/<slug>/`[^engine].
5. **Platform branch.**
   - GBA goes to `_import_gba` (luvdis, `arch: thumb`, default `agbcc`).
   - Game Boy goes to `_import_gb` (SM83, `gbdk-2020` when installed).
   - Everything else is MIPS[^engine].
6. **Split (MIPS).** `split.split_rom` runs splat `create_config`, then `splat split` (spimdisasm). ELF programs are flattened with objcopy. The function returns functions, data symbols, segments, the guessed compiler and `$gp` usage. Overlays found on the disc are passed in[^split][^engine].
7. **Compiler profile.** The toolchain is picked from `DEFAULT_TOOLCHAIN[res.compiler]` (KMC, IDO, PSYQ, EEGCC or AGBCC) unless the caller passed one. It is stored with provenance `PROVENANCE_GUESS`: a hypothesis, not a fact[^engine]. See [toolchains](/project/toolchains.md).
8. **Store.** One transaction inserts these rows[^engine]:
   - the project row, with a config holding the ROM info, segments, compiler and a legal record (`basis: personal-copy`)
   - one `functions` row per function, holding the target bytes, relocations, asm, unit (segment), vram and ROM offset
   - every symbol, with a data preview
   - context header version 1 (basic types)
   - the ROM identities
   - an `import` event
9. **Drafts.** m2c later turns a function's target code into a first C draft, run as a separate program and never imported[^seeds].

Symbols carry automatic names that encode their address (`func_80001234`, `D_80012340`). Because matching works on addresses, renaming never breaks a match[^symbols].

Next: [matching and scoring](/project/matching-and-scoring.md). Per-platform details: [platforms](/project/platforms.md).

[^engine]: src/decompnet/app/engine.py
[^detect]: src/decompnet/rom/detect.py
[^split]: src/decompnet/rom/split.py
[^symbols]: src/decompnet/rom/symbols.py
[^chd]: src/decompnet/rom/chd.py
[^forkengine]: src/decompnet/app/forkengine.py
[^chat]: src/decompnet/app/chat.py
[^seeds]: src/decompnet/seeds.py
