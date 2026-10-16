# ndstool

Nintendo DS ROM packer and unpacker from devkitPro. It builds `.nds` ROMs from ARM9/ARM7 binaries, NitroFS folders and banner data, and extracts them again, which is what DS decomp work needs to check that a rebuilt ROM layout matches the original.

Repo: https://github.com/devkitPro/ndstool
Version: release 2.3.1, commit 76e8b681bb225d945a48852821e03114e6c7ce1c (source cloned and built in a scratch folder, not shipped).
Install: compiled from the upstream C++ sources with clang++ (macOS arm64, native). Only the `ndstool` binary is kept in `bin/`.

Run: `local-tools/ndstool/bin/ndstool -i game.nds` (show header info), `-x` (extract), `-c` (create).

Test: `bash local-tools/ndstool/test.sh` assembles tiny ARM9/ARM7 programs, packs them with a NitroFS file into a ROM, reads the header back, extracts everything and compares bytes. Result: PASS (2026-10-16).
