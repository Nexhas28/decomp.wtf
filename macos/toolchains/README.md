# Native Apple Silicon compilers

The period compilers decomp.wtf runs, built natively for macOS arm64 and shipped with the app in `prebuilt/`,
so that no compiler needs Docker or Rosetta on an Apple Silicon Mac.

| Archive | Toolchain(s) | Source |
|---|---|---|
| `binutils-2.6-macos-arm64.tar.gz` | assembler for gcc-2.7.2-kmc, gcc-2.7.2-psx, gcc-2.8.1-psx | decompals/mips-binutils-2.6 @54514de |
| `gcc-2.7.2-kmc-macos-arm64.tar.gz` | gcc-2.7.2-kmc (N64) | decompals/mips-gcc-2.7.2 @43d1cdb |
| `gcc-2.7.2-psx-macos-arm64.tar.gz` | gcc-2.7.2-psx (PS1) | GNU gcc 2.7.2 + decompals/old-gcc 0.17 patches |
| `gcc-2.8.1-psx-macos-arm64.tar.gz` | gcc-2.8.1-psx (PS1) | GNU gcc 2.8.1 + decompals/old-gcc 0.17 patches |
| `agbcc-macos-arm64.tar.gz` | agbcc (GBA): agbcc, old_agbcc, agbcc_arm | pret/agbcc @da598c1 (+ include/, lib/ from pret's release) |

All of them are GPL; the sources are the pinned ones in `build.sh`.

- `build.sh` rebuilds the archives from pinned sources (Apple's command line tools are enough). After a rebuild,
  copy the printed sha256s into `src/decompnet/toolchains.toml`.
- `verify.sh` compiles zlib, `tests/floats.c`, `tests/warns.c` and csmith random programs with these compilers and
  with the Linux builds (in Docker), and compares the objects byte for byte. Developer check only.

The Mac-only changes, and why, are described at the top of `build.sh`. Toolchains that still have no native build:
IDO 5.3/7.1 is already universal (from ido-static-recomp); EE-GCC 2.96 (PS2) is a Sony binary with no published
source; the Metrowerks compilers are closed-source Windows programs run by wibo.
