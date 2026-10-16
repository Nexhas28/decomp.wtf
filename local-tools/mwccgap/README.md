# mwccgap (MWCC Global Assembly Processor)

Lets a C file compiled with Metrowerks CodeWarrior (MWCC) keep some functions as `INCLUDE_ASM("dir", name)`: they are compiled as nops, then the real instructions (and `.rodata`) from `dir/name.s` are transplanted into the object. This is the "Ship of Theseus" workflow (like asm-processor, but for MWCC, first used for PSP projects) and is useful for decomp.wtf PSP/MWCC matching projects.

- Run: `local-tools/mwccgap/run input.c output.o --mwcc-path <mwcc> --as-path <mips-as> [--use-wibo] [mwcc flags]`
- Install: upstream source copied to `src/` (repo https://github.com/mkst/mwccgap, commit 147598b36b198f267e80adbe04dd5804d070dbb3). Pure-Python, stdlib only, needs Python 3.9+; `run` is a wrapper around `src/mwccgap.py`.
- Needs externally: the proprietary MWCC compiler (not shipped; `--mwcc-path`, optionally via wibo) and a GNU MIPS assembler.
- Test: `bash local-tools/mwccgap/test.sh`. It uses `fake-mwcc.py` (our own tiny stand-in that writes a MWCC-shaped MIPS ELF) plus the decomp.wtf binutils-mips assembler, runs the full pipeline, disassembles the result, and checks the C function is untouched and the INCLUDE_ASM function holds the hand-written instructions. Also checks the missing-.s error path and runs the 16 upstream unit tests. Caveat: a real MWCC run was not tested.
- Result: PASS (macos-arm64 native).
- Gotcha: when stdin is not a TTY, mwccgap reads C source from stdin; scripts must pass `< /dev/null` when giving a file path.
