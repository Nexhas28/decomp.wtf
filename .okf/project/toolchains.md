---
type: Toolchain Registry
title: Pinned toolchain registry (toolchains.toml)
description: The format of src/decompnet/toolchains.toml, how toolchains are installed and verified, and a table of every toolchain id.
tags: [toolchains, compilers, registry, sha256, licenses]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: toml
    resource: src/decompnet/toolchains.toml
    title: Toolchain registry
  - id: py
    resource: src/decompnet/toolchains.py
    title: Registry loader and installer
  - id: profiles
    resource: src/decompnet/compiler_profiles.py
    title: Per-function compiler selection
  - id: appcli
    resource: src/decompnet/app/cli.py
    title: decomp setup
---

# Pinned toolchain registry (toolchains.toml)

## Format

Each top-level TOML table is a toolchain id (for example `["gcc-2.7.2-kmc"]`)[^toml]. The fields map onto the `Toolchain` dataclass in `toolchains.py`[^py]:

- `name`, `license`, `source`, `description`
- `kind`: `compiler` (the default when absent), `runtime`, `tool`, `compiler-pack` or `template`
- `platform` and `arch`, for compilers: for example `N64`/`mips`, `PS1`/`mipsel`, `GBA`/`arm`
- `cc` and `flags`: the compile command and default flags. `{tc}` is the install directory. `cmd` is a full command template.
- `requires`: other toolchains mounted alongside, for example `runtime-i386`
- `parts`: a list of `{url, sha256, platforms, subdir}` archives
- `git` + `commit`, for tools installed from a git commit (m2c, luvdis). Also `binary_name`, `pip_extra` and `requires_java` (Ghidra).

**Verification.** Every part is downloaded from upstream and checked against its pinned sha256 before extraction. A mismatch raises `ToolchainError`. An empty sha256 means trust-on-first-use, and the installer records the digest[^toml][^py].

**Platforms.** Parts list `linux-x86_64`, `linux-aarch64`, `macos-x86_64`, `macos-arm64`, `windows-x86_64` or `wsl-x86_64`. A Mac installs only the `macos-*` parts natively; Apple Silicon runs the x86_64 builds through Rosetta 2. The `linux-*` and `windows-*` parts stay listed because every part is part of a toolchain's identity, and removing one would make existing scores and drafts stale[^toml]. A part with no `platforms` key applies everywhere: `parts_for` defaults to the current platform[^py]. A toolchain with only a Linux build runs in the compiler container (`via_linux`). There a part tagged `wsl-x86_64` (Debian's build, `DEB_PLATFORM` in the code) replaces the Linux one. The tag keeps its old name so that profile identities do not change. See [host environments](/project/host-environments.md)[^py].

**Proprietary entries** (`license = "proprietary"`) are never installed without `--accept-license`[^toml][^py].

**Install location** (`toolchains.root()`) is chosen in this order[^py]:

1. `$DECOMPNET_TOOLCHAINS`, if set.
2. Otherwise `data_dir()/toolchains`. `data_dir()` is `$XDG_DATA_HOME/decompnet` if that is set, otherwise `~/Library/Application Support/decompnet`.

Linux-only builds go under `root()/linux/<id>`. Each install writes a `.decompnet-toolchain.json` manifest[^py].

**CPU.** The period compilers are x86 programs. `unsupported_cpu()` never refuses a Mac, which runs them on any processor: natively, through Rosetta 2, or emulated in the compiler container. It refuses matching only on other non-x86_64 machines[^py].

**`decomp setup`** installs `gcc-2.7.2-kmc`, `m2c` and `objdiff-cli`. It leaves IDO for the user to accept in the app[^appcli].

**Per-function selection.** `compiler_profiles.py` picks the compiler in this order: function override, then segment range, then global range, then default. Ranges are half-open VRAM intervals[^profiles].

## Every toolchain id

| id | kind | platform / arch | license |
| :-- | :-- | :-- | :-- |
| `runtime-i386` | runtime | (32-bit glibc for old compilers) | LGPL-2.1-or-later |
| `gcc-2.7.2-kmc` | compiler | N64 / mips | GPL-2.0 |
| `gcc-2.7.2-psx` | compiler | PS1 / mipsel | GPL-2.0 |
| `gcc-2.8.1-psx` | compiler | PS1 / mipsel | GPL-2.0 |
| `ee-gcc-2.96` | compiler (requires `runtime-i386`) | PS2 / mipsel | GPL-2.0 |
| `ido-7.1` | compiler | N64 / mips | proprietary |
| `ido-5.3` | compiler | N64 / mips | proprietary |
| `mwcc-ps2` | compiler | PS2 / mipsel | proprietary |
| `agbcc` | compiler | GBA / arm | GPL-2.0 |
| `gbdk-2020` | compiler | Game Boy / Game Boy Color / sm83 | MIT (GBDK-2020) + GPL-2.0 (SDCC) |
| `mwcc` | compiler-pack | GameCube/Wii | proprietary |
| `mwccarm` | compiler-pack | DS | proprietary |
| `m2c` | tool (git) | | GPL-3.0 |
| `cloudflared` | tool | | Apache-2.0 |
| `objdiff-cli` | tool | | MIT OR Apache-2.0 |
| `dtk` | tool | | MIT OR Apache-2.0 |
| `wibo` | tool | | MIT |
| `coddog` | tool | | MIT OR Apache-2.0 |
| `gc-wii-binutils` | tool | | GPL-3.0 |
| `maspsx` | tool | | MIT |
| `ghidra` | tool (needs Java) | | Apache-2.0 |
| `binutils-mips` | tool | | GPL-2.0 |
| `dsd` | tool | | MIT |
| `sjiswrap` | tool | | Apache-2.0 |
| `dtk-template` | template | | CC0-1.0 |
| `arm-gnu` | tool | | GPL-3.0 |
| `luvdis` | tool (git) | | MIT |
| `mgbdis` | tool | | MIT |
| `rgbds` | tool | Game Boy / Game Boy Color / sm83 | MIT |

All rows come from `toolchains.toml`[^toml]. `mwcc`, `mwcc-ps2` and `mwccarm` are Windows programs run through the macOS build of `wibo`[^toml]. For what each external tool does, see the [tool concepts](/tools/index.md).

[^toml]: src/decompnet/toolchains.toml
[^py]: src/decompnet/toolchains.py
[^profiles]: src/decompnet/compiler_profiles.py
[^appcli]: src/decompnet/app/cli.py
