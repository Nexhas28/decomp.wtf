---
type: Tool
title: ido-static-recomp (recomp)
description: Static recompiler that turns IRIX MIPS executables (such as the IDO 5.3/7.1 compiler passes) into native C programs for macOS and Linux.
resource: https://github.com/decompals/ido-static-recomp
tags: [n64, irix, mips, static-recompiler, toolchain]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:10:00Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:10:00Z }
sources:
  - id: repo
    resource: https://github.com/decompals/ido-static-recomp
    title: ido-static-recomp README
---

# ido-static-recomp (recomp)

ido-static-recomp converts selected IRIX C toolchain programs into modern Linux or macOS programs. It does this by recompiling the IDO 5.3 and 7.1 passes (cc, cfe, uopt, ugen, as1 and others) ahead of time into C [^repo]. These are the SGI compilers used by most first-party N64 games, so a native, fast IDO is what makes byte-matching N64 decompilation practical. The `recomp` tool itself is open source. The IRIX binaries it is fed are proprietary, so this install keeps only `recomp` and its libc emulation runtime.

## Local install
Path `local-tools/ido-static-recomp/`, commit d5aec59932034b8a20f23f471297a55c45dcbc45 (v1.2-17), entry point `bin/recomp`, runtime sources in `runtime/`. Runs natively on macOS arm64. The prebuilt IDO compilers the project uses are listed separately in the toolchain registry.

## Examples
```bash
bin/recomp t.elf > t.c
cc -O1 -w -I runtime -DIDO71 -DPACKAGE_VERSION='"x"' -DDATETIME='"x"' \
  t.c runtime/libc_impl.c runtime/version_info.c -o tnative
```

## Test result
pass, 2026-10-16. `test.sh` (needs Homebrew llvm@21 and lld@21) links a tiny self-written big-endian MIPS program shaped like an IRIX dynamic executable, in which main calls add3(39). It runs `recomp` on that program, checks that the generated C has `f_main` and the `f_add3` call, then compiles the C natively with the runtime and checks that the exit code is 42.

## Related
[IDO toolchains](/project/toolchains.md), [asm-processor](/tools/asm-processor.md), [asm-differ](/tools/asm-differ.md), [decomp-permuter](/tools/decomp-permuter.md)

[^repo]: ido-static-recomp README
