# local-tools

Helper scripts plus third-party decomp / reverse-engineering tools, each downloaded from GitHub, pinned and smoke-tested on macOS arm64.

- `decomp_local.py` - talks to a running local decomp.wtf instance (list / detail / try / diff / import ...).
- `manifest.json` - every tool below with repo, version, platform, entry point and test status.
- `<tool>/test.sh` - re-runnable smoke test (`bash local-tools/<tool>/test.sh`, exit 0 = works).
- `run-all-tests.sh` - runs every smoke test and prints a pass/fail summary.

Knowledge base entries for each tool live in `.okf/tools/`.

## In decomp.wtf

decomp.wtf finds this folder (`DECOMP_LOCAL_TOOLS`, which the Mac app sets, or `local-tools/` in the checkout it runs
from) and uses it in two places (`src/decompnet/app/localtools.py`):

- **Compilers & tools page:** every tool below with whether it works on this computer, and a Check button that runs
  its `test.sh`.
- **Helper agents:** the MCP tools `local_tools` (what is here) and `run_tool` (run one). Agents have no shell, so they
  only get fixed operations whose command lines decomp.wtf builds from checked values (files inside the project,
  numbers, hex, names from a list): disassemble, decompile (rz-ghidra, RetDec), p-code, decompress (crunch64), find
  library functions (n64sym), display lists, textures, ROM/disc extraction, map files, Ghidra diffs. Results go to
  `tool-output/` in the project. Tools that need free-form input (assemblers writing their own outputs, compiler
  wrappers, Python libraries, emulators, Ghidra extensions) are listed for people with the reason.

| Tool | Category | Status | Repo | Version | Test |
|---|---|---|---|---|---|
| [armips](./armips/) | assembler | pass | https://github.com/Kingcom/armips | 62adab4ef30da765f5cf22a451eb08a59c54dc8b | `armips/test.sh` |
| [asm-differ](./asm-differ/) | assembly-differ | pass | https://github.com/simonlindholm/asm-differ | 0dd09af8f8008f1f880327cf0aca3b26d2562ea2 | `asm-differ/test.sh` |
| [asm-processor](./asm-processor/) | compiler-wrapper | pass | https://github.com/simonlindholm/asm-processor | 1.0.1 | `asm-processor/test.sh` |
| [binwalk](./binwalk/) | file-carving, firmware-analysis, decompression | pass | https://github.com/ReFirmLabs/binwalk | 3.1.0 | `binwalk/test.sh` |
| [ccc](./ccc/) | ps2-symbols | pass | https://github.com/chaoticgd/ccc | v2.1 | `ccc/test.sh` |
| [crunch64](./crunch64/) | compression | pass | https://github.com/decompals/crunch64 | 0.6.2 | `crunch64/test.sh` |
| [decomp-permuter](./decomp-permuter/) | matching-search | pass | https://github.com/simonlindholm/decomp-permuter | 8556c81d80d1c1af98a858c8f4dc951357f29139 | `decomp-permuter/test.sh` |
| [ds-rom](./ds-rom/) | nds-rom-extract-build | pass | https://github.com/AetiasHax/ds-rom | v0.8.0 | `ds-rom/test.sh` |
| [findcode](./findcode/) | rom-analysis | fail | https://github.com/decompals/findcode | 07472cd381e5e3029c33838c45e763cf76b697a6 | `findcode/test.sh` |
| [gba-ghidra-loader](./gba-ghidra-loader/) | ghidra-extension | pass | https://github.com/pudii/gba-ghidra-loader | 1.1.0 (commit 9bfb2d1fe891aa78bab7093a328feb3a52318ab7, patched for Ghidra 12.1.4) | `gba-ghidra-loader/test.sh` |
| [gbadisasm](./gbadisasm/) | disassembler | pass | https://github.com/jiangzhengwenjz/gbadisasm | 940def2c91d678ee671716b8b44a2223c0ff2c62 | `gbadisasm/test.sh` |
| [gclib](./gclib/) | gamecube-file-formats | pass | https://github.com/LagoLunatic/gclib | 4e06db0004df411f1c806ad39b0e9a18ac4a1de5 | `gclib/test.sh` |
| [gfxdis](./gfxdis/) | disassembler | pass | https://github.com/glankk/libgfxd | gfxdis-0.7 (self-reported by binary; no tag or commit verified) | `gfxdis/test.sh` |
| [ghidra-delinker](./ghidra-delinker/) | ghidra-extension | pass | https://github.com/boricj/ghidra-delinker-extension | v0.9.2 (commit b3890c4443e4ab802c26b66d84821d3ac9362c18, Ghidra 12.1.4 build) | `ghidra-delinker/test.sh` |
| [ghidra-emotionengine](./ghidra-emotionengine/) | ghidra-extension | pass | https://github.com/chaoticgd/ghidra-emotionengine-reloaded | v2.1.38 (commit fcc5deac45d8f5c27c632bfe0db928630939676e), release build for Ghidra 12.1.4 | `ghidra-emotionengine/test.sh` |
| [ghidra-gamecube-loader](./ghidra-gamecube-loader/) | ghidra-extension | pass | https://github.com/Cuyler36/Ghidra-GameCube-Loader | 1.3.1 (commit 921504c9ddba6e8d9b3655b665a60f1a33306220, Ghidra 12.1 build) | `ghidra-gamecube-loader/test.sh` |
| [ghidra-n64-loader](./ghidra-n64-loader/) | ghidra-extension | pass | https://github.com/zeroKilo/N64LoaderWV | 12.1.3 (commit 2bd537627513f0dd32678207c8dd3278016d6003, rebuilt for Ghidra 12.1.4) | `ghidra-n64-loader/test.sh` |
| [ghidra-nds-loader](./ghidra-nds-loader/) | ghidra-extension | pass | https://github.com/pedro-javierf/NTRGhidra | v1.5.1 (commit 9e0674c0b36ef9b816dbe7833a68b17327edc1b9, patched for headless use, built for Ghidra 12.1.4) | `ghidra-nds-loader/test.sh` |
| [ghidra-psx-ldr](./ghidra-psx-ldr/) | ghidra-extension | pass | https://github.com/lab313ru/ghidra_psx_ldr | 2026.10.03 | `ghidra-psx-ldr/test.sh` |
| [ghidraboy](./ghidraboy/) | ghidra-extension | pass | https://github.com/Gekkio/GhidraBoy | 20250830 (upstream release, GhidraBoy-1.7.1) + local port to Ghidra 12.1.4 (ghidra-12.1-port.patch) | `ghidraboy/test.sh` |
| [ghidriff](./ghidriff/) | binary-diffing | pass | https://github.com/clearbluejar/ghidriff | v1.0.0 | `ghidriff/test.sh` |
| [ido-static-recomp](./ido-static-recomp/) | static-recompiler | pass | https://github.com/decompals/ido-static-recomp | d5aec59932034b8a20f23f471297a55c45dcbc45 | `ido-static-recomp/test.sh` |
| [keystone](./keystone/) | assembler | pass | https://github.com/keystone-engine/keystone | 0.9.2 | `keystone/test.sh` |
| [mapfile-parser](./mapfile-parser/) | linker-map-parser | pass | https://github.com/ethteck/mapfile_parser | 2.9.0 | `mapfile-parser/test.sh` |
| [mgba](./mgba/) | emulator-debugger | pass | https://github.com/mgba-emu/mgba | 143adf5 (upstream dev build, reports 0.11-1-143adf5; latest tag 0.10.5) | `mgba/test.sh` |
| [mkpsxiso](./mkpsxiso/) | disc-image | pass | https://github.com/Lameguy64/mkpsxiso | v2.30 | `mkpsxiso/test.sh` |
| [mwccgap](./mwccgap/) | global-asm-processor | pass | https://github.com/mkst/mwccgap | 147598b36b198f267e80adbe04dd5804d070dbb3 | `mwccgap/test.sh` |
| [n64recomp](./n64recomp/) | recompiler | pass | https://github.com/N64Recomp/N64Recomp | ffb39cdad1da5de07eaaa48bd1db4a89a7986771 | `n64recomp/test.sh` |
| [n64sym](./n64sym/) | symbol-identification | pass | https://github.com/shygoo/n64sym | ccf4600f3389f1a84bde23339225cf372fdf7712 | `n64sym/test.sh` |
| [ndspy](./ndspy/) | nds-python-library | pass | https://github.com/RoadrunnerWMC/ndspy | v4.2.0 | `ndspy/test.sh` |
| [ndstool](./ndstool/) | rom-packer | pass | https://github.com/devkitPro/ndstool | 76e8b681bb225d945a48852821e03114e6c7ce1c | `ndstool/test.sh` |
| [nodtool](./nodtool/) | disc-image-extract-convert | pass | https://github.com/encounter/nod | v2.0.0-alpha.12 | `nodtool/test.sh` |
| [pcsx-redux-tools](./pcsx-redux-tools/) | object-converter | pass | https://github.com/grumpycoders/pcsx-redux | e7ced7b681296ace22f7847f7bb3c9a206315571 | `pcsx-redux-tools/test.sh` |
| [pigment64](./pigment64/) | texture-converter | pass | https://github.com/decompals/pigment64 | 0.6.3 (release binary) | `pigment64/test.sh` |
| [pret-gba-tools](./pret-gba-tools/) | asset-converter | pass | https://github.com/pret/pokeemerald | 731ad5bfd6e6f265508d0efcca0ba42f9dcf5881 | `pret-gba-tools/test.sh` |
| [pygfxd](./pygfxd/) | disassembler | pass | https://github.com/ethteck/pygfxd | fd11c99 | `pygfxd/test.sh` |
| [pypcode](./pypcode/) | disassembler | pass | https://github.com/angr/pypcode | v4.0.1 | `pypcode/test.sh` |
| [radare2](./radare2/) | disassembler-framework | pass | https://github.com/radareorg/radare2 | 6.2.4 | `radare2/test.sh` |
| [retdec](./retdec/) | decompiler | pass | https://github.com/avast/retdec | v5.0 | `retdec/test.sh` |
| [rizin](./rizin/) | disassembler | pass | https://github.com/rizinorg/rizin | v0.9.1 | `rizin/test.sh` |
| [rz-ghidra](./rz-ghidra/) | ghidra-decompiler-plugin | pass | https://github.com/rizinorg/rz-ghidra | unrecorded build (plugins built against rizin 0.9.1); upstream latest release v0.9.0, default branch dev head 0f4e062 (2026-10-12) | `rz-ghidra/test.sh` |
| [slinky](./slinky/) | linker-script-generator | pass | https://github.com/decompals/slinky | 0.6.0 | `slinky/test.sh` |
| [torch](./torch/) | asset-extractor | pass | https://github.com/HarbourMasters/Torch | v1.0.0 | `torch/test.sh` |
| [unicorn](./unicorn/) | emulator | pass | https://github.com/unicorn-engine/unicorn | 2.1.4 | `unicorn/test.sh` |
