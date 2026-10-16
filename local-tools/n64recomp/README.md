# n64recomp

N64Recomp is a static recompiler that turns N64 MIPS machine code into portable C, with one function per output file, named from an ELF's symbols. RSPRecomp, built from the same repo, does the same job for N64 RSP microcode. Both are useful for decomp.wtf because they let a matched or partly matched function be run and inspected as readable C.

Source: https://github.com/N64Recomp/N64Recomp (MIT), pinned commit ffb39cdad1da5de07eaaa48bd1db4a89a7986771.

How to run:
- `bin/N64Recomp <config.toml>` reads `[input]` with `elf_path` and `output_func_path` (optional `output_binary_path`, `entrypoint`, and so on).
- `bin/RSPRecomp <config.toml>` reads top-level keys `text_offset`, `text_size`, `text_address`, `rom_file_path`, `output_file_path`, `output_function_name`.

Install: built natively on macOS arm64 with CMake 4.4.2 (Apple clang 21) in a scratch build dir; only the two CLI binaries are copied in. They link only against system libraries. No Rosetta or Docker needed.

Test: `bash test.sh` builds a tiny big-endian MIPS ELF and a tiny RSP blob, runs both tools, and checks the emitted C. Last run: PASS (exit 0).
