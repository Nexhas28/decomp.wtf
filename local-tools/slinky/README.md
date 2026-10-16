# slinky

Linker script generator from decompals (splat link yutility). It reads a small YAML file that lists each segment and the object files that belong in it, then writes a GNU ld / LLVM lld linker script. This is the step that makes a decomp project link back into the same layout as the original ROM, so the output can be byte-matched.

Why it helps decomp.wtf: it replaces hand-maintained linker scripts with a generated one, and the YAML is easier to change when a project adds or moves files.

How to run: `local-tools/slinky/bin/slinky-cli path/to/input.yaml -o out.ld` (omit `-o` to print to stdout).

Installed from the decompals/slinky tag 0.6.0 (commit d01d5c1) with `cargo install --locked --path slinky-cli --root local-tools/slinky`. Installed on macOS arm64, native build.

Test: `bash local-tools/slinky/test.sh`. Last run: PASS. The test writes a two-segment YAML, generates a script, checks the SECTIONS block, the object-path mappings, the fixed VRAM address, stdout output, and that a missing input file fails.
