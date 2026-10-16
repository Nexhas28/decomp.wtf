---
type: Platform
title: Supported platforms and their pipelines
description: For each console, how decomp.wtf splits, disassembles, compiles and judges functions, and how ready the platform is.
tags: [platforms, n64, psx, ps2, gamecube, wii, gba, gb, ds]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: readme
    resource: README.md
    title: Platforms section
  - id: engine
    resource: src/decompnet/app/engine.py
    title: PLATFORM_STATUS, DEFAULT_TOOLCHAIN, import routing
  - id: detect
    resource: src/decompnet/rom/detect.py
    title: Input detection
  - id: split
    resource: src/decompnet/rom/split.py
    title: splat split
  - id: match
    resource: src/decompnet/rom/match.py
    title: MIPS matcher
  - id: matcharm
    resource: src/decompnet/rom/match_arm.py
    title: ARM/Thumb matcher
  - id: gba
    resource: src/decompnet/rom/gba.py
    title: GBA function discovery
  - id: gbbuild
    resource: src/decompnet/rom/gb_build.py
    title: Game Boy SDCC lane
  - id: gbmgbdis
    resource: src/decompnet/rom/gb_mgbdis.py
    title: Game Boy mgbdis split
  - id: scratchdtk
    resource: src/decompnet/forks/scratch_dtk.py
    title: GameCube/Wii from scratch
  - id: scratchdsd
    resource: src/decompnet/forks/scratch_dsd.py
    title: DS from scratch
  - id: overlays
    resource: src/decompnet/rom/overlays.py
    title: PS1/PS2 overlays
  - id: n64code
    resource: src/decompnet/rom/n64code.py
    title: N64 extra code segments
  - id: maspsx
    resource: src/decompnet/rom/maspsx.py
    title: maspsx delay-nop pass
---

# Supported platforms and their pipelines

`PLATFORM_STATUS` in the engine marks `gc`, `wii` and `n64` as **recommended**, and `psx`, `ps2`, `gba`, `gb`, `gbc` and `nds` as **beta**[^engine]. The README lists 3DS, PSP, Switch, PC, Xbox and Xbox 360 as "coming" for from-scratch work. They are reachable today only as forks of public decomps[^readme].

| Platform | Input detected | Split / disassembly | Default compiler | Judge |
| :-- | :-- | :-- | :-- | :-- |
| N64 | `.z64/.n64/.v64` ROM[^detect] | splat `create_config` + `split` (spimdisasm)[^split]. `n64code.py` finds code loaded later by working out where its `jal` targets land[^n64code]. | KMC → `gcc-2.7.2-kmc`, IDO → `ido-7.1`[^engine] | Candidate linked at the function's address and compared with the ROM (`rom/match.py`)[^match] |
| PlayStation | PS-X EXE, or a disc as `.bin/.iso/.chd`[^detect] | splat on the program. Overlays are found in other disc files[^overlays]. | PSYQ → `gcc-2.7.2-psx`[^engine] | MIPS matcher. An opt-in maspsx delay-nop pass is available[^maspsx]. |
| PlayStation 2 | EE ELF, or a disc[^detect] | splat, after flattening the ELF with objcopy from `binutils-mips`[^split] | EEGCC → `ee-gcc-2.96`[^engine] | MIPS matcher |
| Game Boy Advance | `.gba` ROM[^detect] | luvdis, plus a second pass for Thumb prologues nothing calls[^gba] | `agbcc`[^engine] | `rom/match_arm.py`: ARM/Thumb linked at its address[^matcharm] |
| Game Boy / Color | `.gb/.gbc` ROM[^detect] | built-in SM83 decoder, or mgbdis + rgbds when installed (verified by reassembly)[^gbmgbdis] | `gbdk-2020` (SDCC). Without it, functions import as assembly only[^engine]. | `gb_build.py`: `sdcc -S` → `sdasgb -l` → listing bytes[^gbbuild] |
| GameCube / Wii | disc image or DOL[^engine] | decomp-toolkit inside a generated dtk-template project, REL modules included[^scratchdtk] | CodeWarrior (`mwcc` pack) | objdiff in the project's own ninja build; files are linked once they fully match[^scratchdtk] |
| Nintendo DS | `.nds` ROM[^engine] | ds-decomp (`dsd`): ARM9, ITCM, DTCM, overlays[^scratchdsd] | CodeWarrior for DS (`mwccarm`) | objdiff; ROM rebuilt and checked on every build[^scratchdsd] |

## Notes

- GameCube/Wii discs and DS ROMs are detected by `_community_layout` and routed to `forks.create_scratch`. They become fork-style projects, not ROM projects[^engine].
- PS1/PS2 imports drop the toolchain's `-G` flags and use `-G8` or `-G0`, depending on whether splat found a `$gp`[^engine].
- C++ is supported for GameCube, Wii and DS. On the other platforms, functions started from scratch are written in C[^readme].
- Compiler choice per function can be overridden by ranges. See [toolchains](/project/toolchains.md).

[^readme]: README.md
[^engine]: src/decompnet/app/engine.py
[^detect]: src/decompnet/rom/detect.py
[^split]: src/decompnet/rom/split.py
[^match]: src/decompnet/rom/match.py
[^matcharm]: src/decompnet/rom/match_arm.py
[^gba]: src/decompnet/rom/gba.py
[^gbbuild]: src/decompnet/rom/gb_build.py
[^gbmgbdis]: src/decompnet/rom/gb_mgbdis.py
[^scratchdtk]: src/decompnet/forks/scratch_dtk.py
[^scratchdsd]: src/decompnet/forks/scratch_dsd.py
[^overlays]: src/decompnet/rom/overlays.py
[^n64code]: src/decompnet/rom/n64code.py
[^maspsx]: src/decompnet/rom/maspsx.py
