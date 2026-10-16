---
type: Tool
title: mwccgap
description: Global assembly processor for MWCC that compiles INCLUDE_ASM functions as nops and transplants the real assembled instructions into the object.
resource: https://github.com/mkst/mwccgap
tags: [psp, mips, mwcc, global-asm, preprocessor]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:08:00Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:07:22Z }
sources:
  - id: repo
    resource: https://github.com/mkst/mwccgap
    title: mwccgap README
---

# mwccgap

mwccgap lets individual functions in a C file be replaced by assembly during a Metrowerks CodeWarrior (MWCC) build. Functions marked with an `INCLUDE_ASM("dir", name)` macro are compiled as a run of nops of the right size, then the code (and any `.rodata`) assembled from `dir/name.s` is transplanted into the compiled object, with symbols and relocations fixed up [^repo]. This makes the "Ship of Theseus" approach to byte-matching decompilation practical with MWCC: you can decompile one function at a time while the rest stays as assembly, much like asm-processor does for IDO/GCC [^repo].

## Local install
Path `local-tools/mwccgap/`, upstream commit `147598b36b198f267e80adbe04dd5804d070dbb3`, source copy in `src/`, entry point `local-tools/mwccgap/run` (wrapper around `src/mwccgap.py`, stdlib-only Python). Runs natively on macos-arm64. The real MWCC compiler is proprietary and not included; it must be supplied with `--mwcc-path` (optionally through wibo with `--use-wibo`), plus a GNU MIPS assembler with `--as-path`.

## Examples
```bash
# </dev/null is needed in scripts: mwccgap reads C from stdin when stdin is not a TTY
local-tools/mwccgap/run test.c out.o --mwcc-path local-tools/mwccgap/fake-mwcc.py \
  --as-path mips-ps2-decompals-as --as-march mips32 \
  --macro-inc-path macro.inc --asm-dir-prefix . </dev/null
```

## Test result
Pass, 2026-10-16. `local-tools/mwccgap/test.sh` uses a small stand-in compiler (`fake-mwcc.py`, written for the test) and the binutils-mips assembler to run the whole pipeline, disassembles the output with objdump, and checks that the plain C function is untouched and the INCLUDE_ASM function contains the hand-written instructions instead of nops. It also checks that a missing `.s` file makes mwccgap fail and runs the 16 upstream unit tests. Not verified against real MWCC output.

## Related
[asm-processor](/tools/asm-processor.md), [splat](/project/toolchains.md), [wibo](/project/toolchains.md)

[^repo]: mwccgap README
