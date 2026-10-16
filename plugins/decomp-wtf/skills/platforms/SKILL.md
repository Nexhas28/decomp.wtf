---
name: platforms
description: Which decomp.wtf guides to read for a project's platform and compiler (N64 KMC/IDO, PS1/PS2, GBA, Game Boy, GameCube/Wii/DS forks). Read once at the start of work on a project, or when a function hits a platform-specific problem.
---

# Platform lanes

`project_info()` names the platform and compiler. Read the lane's guides once per session with `guide(topic)`
(each is under 6 KB). If a guide's release count differs from the stall note, the stall note wins.

- **ROM project** (N64, PS1, PS2, GBA, Game Boy): your C plus `context.h` is compiled, linked at the function's
  address and compared with the ROM.
- **Fork** (public decomp, or GameCube/Wii/DS): your definition replaces the function in the project's source;
  objdiff judges it. Tools: `read_file`, `search_code`, `edit_file`, `file_status`. Follow the project's style
  and its rules on AI contributions.

| Lane | Read first | On demand |
|---|---|---|
| N64, KMC GCC 2.7.2 | `mips`, `gcc-kmc` | `n64`, `data`, `structs` |
| N64, IDO | `mips`, `ido` | `n64`, `data`, `structs` |
| PS1 (PSY-Q GCC) | `mips`, `psx`, [references/psx.md](references/psx.md) | `data`, `structs` (R3000 has no branch-likely) |
| PS2 | `mips`, `ps2` | `data`, `structs` |
| GBA (agbcc) | `arm`, `agbcc` | `data`, `structs` |
| Game Boy / Color | `gb`, [references/gb.md](references/gb.md) | `data` |
| Any fork | `fork`, `objdiff` | GC/Wii `ppc` `mwcc` `cpp`; DS `arm` `mwcc`; PSP `mips`; 3DS `arm` `cpp`; Switch `aarch64` `cpp`; PC/Xbox `x86` `cpp`; Xbox 360 `ppc` `cpp` |

Always available: `workflow`, `m2c`, `naming`, `similarity`. Name things by symbol; an address is written `0x…`
(on Game Boy use names, banks share addresses).
