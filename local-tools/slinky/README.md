# slinky

Linker script generator from decompals (splat link yutility). It reads a small YAML file that lists each segment and the object files that belong in it, then writes a GNU ld / LLVM lld linker script. This is the step that makes a decomp project link back into the same layout as the original ROM, so the output can be byte-matched.

Why it helps decomp.wtf: it replaces hand-maintained linker scripts with a generated one, and the YAML is easier to change when a project adds or moves files.

How to run: `local-tools/slinky/bin/slinky-cli path/to/input.yaml -o out.ld` (omit `-o` to print to stdout).

Installed from the decompals/slinky GitHub release 0.6.0: the prebuilt Apple Silicon binary `slinky-cli-aarch64-apple-darwin.tar.gz` (sha256 4c2c0db5a9f185cfd02d4a0fb4865b506a5a76dda6f008677b5adaaef1c07fb9), copied into `bin/`. No build needed.

Test: `bash local-tools/slinky/test.sh`. Last run: PASS. The test writes a two-segment YAML, generates a script, checks the SECTIONS block, the object-path mappings, the fixed VRAM address, stdout output, and that a missing input file fails.
