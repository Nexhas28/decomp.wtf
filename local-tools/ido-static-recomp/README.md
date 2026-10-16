# ido-static-recomp (recomp tool only)

`bin/recomp` is the static recompiler from decompals/ido-static-recomp: it translates a big-endian MIPS IRIX dynamic ELF executable into portable C, which is then compiled natively together with `runtime/libc_impl.c` (an IRIX libc emulation layer). This is how the IDO 5.3/7.1 compilers used by most N64 matching decomps are made to run natively on macOS/Linux.

Why useful for decomp.wtf: lets us rebuild IDO toolchain binaries from source-level tooling (or recompile other IRIX tools) instead of trusting prebuilt release archives. The project already ships the prebuilt IDO recomp releases (see toolchains.toml); this folder holds only the open-source recompiler, no IDO binaries.

Run: `bin/recomp [--conservative] program.elf > program.c`, then `cc -I runtime -DIDO71 -DPACKAGE_VERSION='"x"' -DDATETIME='"x"' program.c runtime/libc_impl.c runtime/version_info.c -o program`.

Installed from https://github.com/decompals/ido-static-recomp at commit d5aec59932034b8a20f23f471297a55c45dcbc45 (v1.2-17), built with `make setup RELEASE=1 MAKE=make CC=clang CXX=clang++` (bundled rabbitizer) in a scratch dir; only `build/recomp.elf` (as `bin/recomp`) and the 5 runtime sources were copied. The full clone also fetched the repo's `ido/` folder (proprietary IRIX binaries); it was never used and the whole scratch clone was deleted afterwards.

Test (`bash test.sh`): needs Homebrew llvm@21 + lld@21. Builds a tiny self-written IRIX-shaped MIPS ELF (main calls add3(39)), recompiles it to C, compiles that natively and checks the exit code is 42. Result: PASS (2026-10-16).
