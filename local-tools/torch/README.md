# Torch

Torch (HarbourMasters, MIT) is a generic asset processor for N64 games. It reads a ROM dump and turns the segmented assets (textures, display lists, vertices, matrices, audio) into an OTR archive or C source, which is what a decomp project needs to check its extracted data against the original.

Use it from the project root: `local-tools/torch/bin/torch pack <folder> <out.otr>` packs a folder into an OTR, and `local-tools/torch/bin/torch binary <baserom.z64>` extracts from a ROM. The ROM mode reads `config.yml` from the current directory, keyed by the ROM hash, so it needs a config entry for that ROM.

Installed from HarbourMasters/Torch tag v1.0.0 (commit bb39229), built with CMake and Apple clang 21 in a scratch directory. Two compile workarounds were needed for this toolchain (bundled fmt consteval and a chained-comparison error in upstream AudioManager.cpp), so only the binary was copied here.

Smoke test (`bash test.sh`): packs a two-file folder, and runs binary mode on a fake N64-shaped file with a matching config. Last run: pass.
