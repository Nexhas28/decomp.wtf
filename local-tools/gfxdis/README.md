# gfxdis

Command-line display list disassembler for N64 Reality Signal Processor (RSP) graphics microcode. It turns raw F3D, F3DEX, F3DEXB and F3DEX2 display list bytes into gbi.h macro calls such as `gsSPDisplayList(...)`, which helps when matching decompiled rendering code to the original.

Five binaries are provided, one per microcode variant: `bin/gfxdis.f3d`, `bin/gfxdis.f3db`, `bin/gfxdis.f3dex`, `bin/gfxdis.f3dex2` and `bin/gfxdis.f3dexb`. Each prints "gfxdis-0.7" as its banner.

Run one with a hex display list: `bin/gfxdis.f3dex2 -d E700000000000000DE00000006001230DF00000000000000`, or a binary file with `-f file.bin`.

Install: the binaries are Mach-O arm64 (native on macOS Apple Silicon). They were already present in `bin/` from an earlier partial attempt. The upstream repo `glankk/gfxdis` returns 404 on GitHub; the closest source is `glankk/libgfxd` (MIT, a display list decompiler library). The source and build steps for these exact binaries are not recorded here, so provenance is unverified (see tool.json notes).

Test: `bash local-tools/gfxdis/test.sh` disassembles a hand-made F3DEX2 display list (hex and file input) and a classic F3D list, and checks for gsDPPipeSync, gsSPDisplayList, gsSPEndDisplayList. Last run: PASS.
