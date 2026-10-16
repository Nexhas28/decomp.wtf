# pygfxd

Python (ctypes) bindings for libgfxd, the N64 Reality Display Processor display-list disassembler. Turns raw F3D/F3DEX2 Gfx words into `gsSP*`/`gsDP*` C macros, which is how N64 display lists are written in decomp projects.

Why it helps decomp.wtf: display lists in N64 decomp targets can be dumped from ROM and rewritten as readable C macros, so they can be compared against the original by hand or in scripts without a full ucode emulator.

Run: `local-tools/pygfxd/run <display-list.bin> [f3dex2|f3d|f3db|f3dex|f3dexb]` (default f3dex2). Input is a raw big-endian display list file.

Install: source is https://github.com/ethteck/pygfxd at commit fd11c99 (2024-01-10, MIT). The bundled `lib/libgfxd.so` was built from the libgfxd submodule sources in that repo with `gcc -shared -o libgfxd.so -Wl,-all_load libgfxd/libgfxd.a`; the repo's own Makefile uses GNU-only linker flags that fail on macOS. `lib/pygfxd.py` is copied unmodified. `.venv/` is a plain uv venv (no extra packages needed; the bindings are stdlib only).

Test: `bash local-tools/pygfxd/test.sh` assembles a two-command display list (G_RDPPIPESYNC, G_ENDDL) and checks the output contains `gsDPPipeSync()` and `gsSPEndDisplayList()`. Status: pass (see tool.json).
