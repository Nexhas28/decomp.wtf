---
name: platforms
description: Which decomp.wtf guides to read and which lane rules apply for a project's platform and compiler (N64 KMC/IDO, PlayStation 1/2, GBA, Game Boy/Color, GameCube/Wii/DS forks, other public decomps), plus field fixes the guides lack. Use at the start of work on a project, or when a function hits a platform-specific problem.
---

# Platform lanes

`project_info()` names the platform and compiler. Read the guides for the lane **once per session** with
`guide(topic)`: they are under 6 KB each and don't change. Where a guide gives a different "release after ~N compiles"
than the stall note in `compile_and_diff`, the stall note wins.

## Two kinds of project
- **ROM project** (a game imported from scratch: N64, PS1, PS2, GBA, Game Boy): your C plus the shared `context.h` is
  compiled, linked at the function's address and compared with the ROM. Header and names: `get_context`,
  `edit_context`, `rename_symbol`; boundaries: `split_function`.
- **Fork** (a public decomp, and GameCube/Wii/DS games started from scratch): your definition replaces the function in
  the project's own source file, the project builds it and objdiff judges it. The goal is linked files. Tools:
  `read_file`, `search_code`, `edit_file`, `file_status`, `split_file`; follow the project's style and its rules on AI
  contributions.

## Guides per lane
| Lane | Read first | On demand |
|---|---|---|
| N64, KMC GCC 2.7.2 | `mips`, `gcc-kmc` | `n64`, `data`, `structs` |
| N64, IDO 5.3/7.1 | `mips`, `ido` | `n64`, `data`, `structs` |
| PlayStation 1 (PSY-Q GCC) | `mips`, `psx`, then [references/psx.md](references/psx.md) | `data`, `structs` (`mips` is written for N64's R4300: its branch-likely advice doesn't apply to the R3000) |
| PlayStation 2 | `mips`, `ps2` | `data`, `structs` |
| GBA (agbcc) | `arm`, `agbcc` | `data`, `structs` |
| Game Boy / Color | `gb`, then [references/gb.md](references/gb.md) | `data` |
| Any fork | `fork`, `objdiff` | plus the platform's: GC/Wii `ppc` `mwcc` `cpp`; DS `arm` `mwcc`; PS1 `mips` `psx`; PS2 `mips` `ps2`; PSP `mips`; 3DS `arm` `cpp`; Switch `aarch64` `cpp`; PC/Xbox `x86` `cpp`; Xbox 360 `ppc` `cpp` |

Every lane: `m2c` (reading the machine draft), `naming`, `similarity` when you need them. `guide()` lists all topics.

## Addresses
Name a function or data by its symbol. An address works too, written `0x…` (a bare hex word is taken as a name
unless it has all 8 digits). On Game Boy several banks share one address, so use names there.
