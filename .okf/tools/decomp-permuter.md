---
type: Tool
title: decomp-permuter
description: Randomised source permuter that searches equivalent C rewrites until a compiled function matches the target assembly byte for byte.
resource: https://github.com/simonlindholm/decomp-permuter
tags: [mips, ido, matching-search]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:03:48Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:04:00Z }
sources:
  - id: repo
    resource: https://github.com/simonlindholm/decomp-permuter
    title: decomp-permuter README
---

# decomp-permuter

decomp-permuter [^repo] automates the last step of matching decompilation. Given a C function that nearly matches a target object file, it generates many equivalent variants (reordered statements, alternative expressions, extra temporaries), compiles each one and scores the resulting assembly against the target. The best variant is kept, and the search stops at score 0, which means a match.

## Local install

Path `local-tools/decomp-permuter/`, upstream commit 8556c81d80d1c1af98a858c8f4dc951357f29139 (main, 2026-10-06). Entry point `run` (Python 3.12 in `.venv/`). Runs natively on macOS arm64. The target compiler is whatever `compile.sh` calls, here IDO 7.1.

## Examples

```bash
local-tools/decomp-permuter/run <folder> --stop-on-zero -j1
```

## Test result

PASS, 2026-10-16. `test.sh` builds a tiny MIPS function with IDO 7.1. It checks that a mismatching target gives a non-zero base score under `--debug`, and that the matching target gives `base score = 0` and `Found zero score` under `--stop-on-zero`.

## Related

- [asm-differ](/tools/asm-differ.md) (used to inspect the diffs the permuter is trying to close)
- [splat](/project/toolchains.md)

[^repo]: decomp-permuter README
