---
type: Pipeline
title: Rebuildable project export
description: How a ROM project is exported as a normal decomp source tree with a build.py that rebuilds the game and checks it byte for byte.
tags: [export, build, rebuild, ports, mods]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: export
    resource: src/decompnet/rom/export.py
    title: MIPS export
  - id: exportbuild
    resource: src/decompnet/rom/export_build.py
    title: Generated build.py (MIPS)
  - id: exportgba
    resource: src/decompnet/rom/export_gba.py
    title: GBA export
  - id: exportgbabuild
    resource: src/decompnet/rom/export_gba_build.py
    title: Generated build.py (GBA)
  - id: engine
    resource: src/decompnet/app/engine.py
    title: export_rom
  - id: readme
    resource: README.md
    title: The base for ports and mods
---

# Rebuildable project export

`RomNetwork.export_rom(slug)` writes a ROM project out as a normal decomp tree[^engine]. The `export_project` chat tool and `/api/export` call it.

## MIPS (N64, PS1, PS2): `rom/export.py`

- **Contents.** splat's assembly for every function and all data (with the project's names), the matched functions as C, a linker script, and a `build.py`[^export].
- **Objects.** Each function is its own object. A matched one is compiled exactly as decomp.wtf verified it; the rest are assembled[^export].
- **Data.** A matched function's own constants are mapped onto the game's copy at verified addresses, so nothing is duplicated[^export].
- **Generated `build.py`**[^exportbuild]:
  - `python build.py`: extract, build, compare
  - `build.py check NAME`: check one function's C
  - `build.py status`: count functions done
- **Linker script.** `game.ld` is regenerated from `build.json` and puts each function at its original address[^exportbuild]. The README notes that a mod that makes code bigger needs a shiftable build, which isn't available yet[^readme].

## GBA: `rom/export_gba.py`

The tree holds luvdis assembly for undecompiled code and agbcc-compiled C for matched functions. It is linked with the Arm GNU toolchain and checked byte for byte. The assembly is regenerated from the user's copy, so it stays out of git[^exportgba][^exportgbabuild].

## GameCube, Wii and DS

These projects already *are* dtk-template or ds-decomp trees that rebuild the game on every build. See [forks](/project/forks.md)[^readme].

Game code and data stay out of git in exports. They are regenerated from the user's own copy[^readme].

[^export]: src/decompnet/rom/export.py
[^exportbuild]: src/decompnet/rom/export_build.py
[^exportgba]: src/decompnet/rom/export_gba.py
[^exportgbabuild]: src/decompnet/rom/export_gba_build.py
[^engine]: src/decompnet/app/engine.py
[^readme]: README.md
