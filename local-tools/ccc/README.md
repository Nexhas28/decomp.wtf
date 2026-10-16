# ccc (chaoticgd/ccc)

ccc is a small C++ library and command line set (`stdump`, `objdump`, `demangle`, `uncc`) that parses debugging symbols from PS2 games, mainly STABS data in `.mdebug` sections. It is useful for decomp.wtf because it lets us read type, function and global names out of PS2 objects and ELF files, which helps when naming decompiled code.

- Entry point: `bin/stdump` (also `bin/objdump`, `bin/demangle`, `bin/uncc`).
- Usage: `bin/stdump functions file.o`, `bin/stdump types file.o`, `bin/stdump globals file.o`, `bin/stdump json file.o`, `bin/stdump identify file.o`.
- Install: prebuilt arm64 Mach-O binaries copied from a local build of the upstream repo (https://github.com/chaoticgd/ccc), latest tag v2.1 (HEAD at check time 88be627a8399bcc084c7ea1161fc0e33780c2e98). Licence files are in `licenses/`.
- Source commit of the copied binaries is not recorded in this folder; the build was done by an earlier attempt and not re-verified beyond the test below.
- Test: `bash test.sh` loads a tiny prebuilt MIPS ELF fixture (`.mdebug` STABS) and checks identify, types, functions, globals, json output and GNU v2 demangling.
- Test result: PASS (see .okf/tools/ccc.md).
