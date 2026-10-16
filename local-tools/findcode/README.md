# findcode

Scans an N64 ROM and reports where the MIPS code regions are, using heuristics (decompals/findcode, Unlicense). Useful for decomp.wtf to find code segments in a ROM before splitting or matching them.

Status: FAILS TO BUILD on this machine (macOS arm64, Apple clang 21). No binary is installed.

- Pinned source: decompals/findcode commit 07472cd (2022-12-16), with lib/fmt at 74c51ff and lib/rabbitizer at 3f6d92b.
- Build attempt 1: pinned fmt. Stops with consteval errors in fmt's format-inl.h (`call to consteval function ... is not a constant expression`).
- Build attempt 2: fmt 11.0.2 swapped in. Stops with the same class of consteval errors in fmt's os.cc.
- The upstream Makefile also runs `mkdir $@ -p`, which fails on macOS mkdir. Overriding it with `MKDIR="mkdir -p" MKDIR_OPTS=` works around that.
- Not tried: forcing `-DFMT_CONSTEVAL=constexpr` as a workaround. Untested.

Run the test: `bash local-tools/findcode/test.sh` (currently exits 1 with the reason above).
