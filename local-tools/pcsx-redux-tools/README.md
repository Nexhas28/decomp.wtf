# pcsx-redux-tools

Two command-line tools from the PCSX-Redux PlayStation 1 emulator project (https://github.com/grumpycoders/pcsx-redux):

- `bin/psyq-obj-parser`: reads Sony PSY-Q SDK `.OBJ` files (the "LNK" object format), prints their sections, symbols and relocations (`-d`), and converts them to standard MIPS ELF objects (`-o out.o`). Useful for decomp.wtf because PS1 games and their SDK libraries were built with PSY-Q, so this lets modern tools (objdiff, binutils, linkers) read original SDK objects.
- `bin/ps1-packer`: loads PS-EXE, ELF, CPE or PSF files and writes compressed PS-EXE, CPE, raw or ROM output. Handy for turning a matching-decomp ELF build into a runnable PS-EXE and for reading CPE/PSF inputs.

Run: `local-tools/pcsx-redux-tools/bin/psyq-obj-parser LIB.OBJ -d` or `local-tools/pcsx-redux-tools/bin/ps1-packer in.elf -o out.ps-exe`.

Install: shallow clone of main at commit e7ced7b681296ace22f7847f7bb3c9a206315571 with only the needed submodules (fmt, ELFIO, ucl, magic_enum, iec-60908b, src/mips), compiled directly with clang++ -std=c++2b -O2 on macOS arm64. Only the binaries were copied in. The emulator itself was not built (it needs many GUI/media libraries).

Test: `bash local-tools/pcsx-redux-tools/test.sh` hand-builds a tiny PSY-Q object and a tiny PS-EXE, converts the object to a MIPS ELF (checks code bytes and symbol) and packs the PS-EXE (checks PS-X EXE and CPE output). Result: PASS (2026-10-16).
