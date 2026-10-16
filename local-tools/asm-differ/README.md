# asm-differ

Assembly diff tool from simonlindholm (Unlicense). It compares the target and current compiled
assembly of a function (or object files) side by side and highlights the instructions that differ,
which is the core loop of byte-matching decompilation for decomp.wtf.

## Install
- Repo: https://github.com/simonlindholm/asm-differ
- Version: commit `0dd09af8f8008f1f880327cf0aca3b26d2562ea2` (pushed 2026-09-06)
- Installed as a Python package into `.venv/` with uv (Python 3.12), so `run` calls `.venv/bin/asm-differ`.

## Run
```bash
local-tools/asm-differ/run -o -f mine.o -F expected.o --no-pager --format plain func
```
Run it from a directory containing a `diff_settings.py` (see `test.sh` for a minimal one).
The smoke test needs MIPS binutils (the decomp.wtf `binutils-mips` cache or `MIPS_AS`).

## Test
`bash local-tools/asm-differ/test.sh` assembles two MIPS objects that differ in one immediate,
checks the JSON score and the plain diff output, then checks that identical objects score 0.
Last run: pass (2026-10-16).
