# RetDec

RetDec is avast's open-source (MIT) machine-code decompiler. It turns a compiled binary (ELF, PE, Mach-O, raw firmware) into C-like code, and supports MIPS, PowerPC and ARM among other architectures.

Why it helps decomp.wtf: it gives a quick first-pass C reading of an unknown binary, which is useful before writing matching code by hand. Its output is not byte-exact, so treat it as a starting point only.

How to run: `local-tools/retdec/run INPUT.elf -o out.c`. The wrapper calls the x86_64 binary through Rosetta 2 (`arch -x86_64`), so Rosetta must be installed.

Install: upstream repo https://github.com/avast/retdec, tag v5.0 (commit 53e55b4b26e9b843787f0e06d867441e32b1604e, verified with `gh api` against the tag). The x86_64 build lives in `bin/` with its `libcrypto` dylib in `lib/` and the support files in `share/retdec/`. The source archive was not kept, so `sha256` is null.

Test: `bash local-tools/retdec/test.sh` builds tiny MIPS, PowerPC and ARM ELF files from a C snippet, decompiles each one, and checks that `add3` is recovered with the expected signature and call. Last run 2026-10-16: PASS (exit 0).
