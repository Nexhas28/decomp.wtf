# n64sym

n64sym identifies known library functions in N64 RAM dumps or ROM images by matching their bytes against signature files, then prints addresses and names in formats such as armips, splat or Project64 `.sym`. It is useful for decomp.wtf because it can name functions in a game's code segment before any matching work starts. The companion tool n64sig builds signature files from ELF objects or libraries.

Run it with `local-tools/n64sym/bin/n64sym <dump.bin> -s` (built-in signatures) or `-l <file.o> -f armips`. Build the signatures with `local-tools/n64sym/bin/n64sig -l <file.o>`.

Installed from https://github.com/shygoo/n64sym at commit ccf4600f3389f1a84bde23339225cf372fdf7712 (master, last pushed 2023-03-27; latest tag v0.9a). Built from a fresh clone in the scratchpad, with three macOS-only fixes that the upstream Makefile lacks: the builtin signature symbol is named with a leading underscore, the static link flag is dropped, and the tools `compress` helper is built without `-static`. Only the `n64sym` and `n64sig` binaries are kept in `bin/`.

Test: `bash local-tools/n64sym/test.sh` assembles two MIPS functions, hides them in a fake RAM dump, and checks that n64sym names both at the right addresses and that n64sig's output works at a new offset. Last result: PASS, 2026-10-16.
