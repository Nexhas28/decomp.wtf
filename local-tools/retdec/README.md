# RetDec

RetDec is avast's open-source (MIT) machine-code decompiler. It turns a compiled binary (ELF, PE, Mach-O, raw firmware) into C-like code, and supports MIPS, PowerPC and ARM among other architectures.

Why it helps decomp.wtf: it gives a quick first-pass C reading of an unknown binary, which is useful before writing matching code by hand. Its output is not byte-exact, so treat it as a starting point only.

How to run: `local-tools/retdec/run INPUT.elf -o out.c`. The wrapper calls the native Apple Silicon binary directly; Rosetta is no longer needed.

Install: Homebrew (`brew install retdec`, Apple Silicon bottle of v5.0): `bin/retdec-decompiler` links to `/opt/homebrew/bin/retdec-decompiler`. Upstream repo https://github.com/avast/retdec, tag v5.0 (commit 53e55b4b26e9b843787f0e06d867441e32b1604e); its own GitHub macOS archive is Intel-only, which is why the earlier install ran under Rosetta (kept aside in `bin.local-build/`).

Test: `bash local-tools/retdec/test.sh` builds tiny MIPS, PowerPC and ARM ELF files from a C snippet, decompiles each one, and checks that `add3` is recovered with the expected signature and call. Last run 2026-10-16: PASS (exit 0).
