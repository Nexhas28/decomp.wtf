---
type: Reference
title: Getting started with decomp.wtf
description: The starting point for this bundle, covering what decomp.wtf is, how to install, run and test it, and where to read next.
tags:
- getting-started
- decomp-wtf
- install
- runbook
status: stable
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: readme
    resource: README.md
    title: decomp.wtf README
  - id: pyproject
    resource: pyproject.toml
    title: Package metadata and scripts
  - id: appcli
    resource: src/decompnet/app/cli.py
    title: decomp CLI
  - id: tools
    resource: src/decompnet/app/tools.py
    title: Agent work loop
---

# Getting started with decomp.wtf

decomp.wtf (Python package `decompnet` 0.10.6, Python 3.12 or newer) lets AI agents decompile games the user owns into C that compiles to byte-identical code. Each attempt is compiled with the game's original, pinned compiler[^pyproject][^readme]. The project is **discontinued**, and the code stays available under MIT[^readme]. Start with the [overview](/project/overview.md).

## Run from a checkout

```bash
uv sync
uv run decomp              # opens the app; projects live in ~/Decomp (or $DECOMP_HOME)
uv run pytest
```

These commands come from the README's Development section[^readme].

- **Ports.** The app listens on 127.0.0.1, port 6464 by default[^appcli].
- **Compilers.** `decomp setup` installs the free compilers and the m2c draft decompiler[^appcli].
- **Demo ROM.** The app ships with the Demo Quest 64 ROM, so no game is needed to try it[^readme].

For this checkout, the test command is `.venv/bin/python -m pytest -q tests`. See [testing](/project/testing.md) for the baseline.

## First decompile

1. Import a game you own in the app, or ask a connected agent (`decomp connect`, then "decompile my game at ...")[^readme].
2. Agents call `project_info`, then repeat `claim_function` → `compile_and_diff` until the score is 0[^tools].

## Read next

- [Project index](/project/index.md): every concept about the codebase.
- [Architecture](/project/architecture.md), [CLI](/project/cli.md), [configuration and state](/project/configuration-and-state.md).
- [Tool concepts](/tools/index.md): splat, m2c, objdiff, Ghidra and the other external tools.

[^readme]: README.md
[^pyproject]: pyproject.toml
[^appcli]: src/decompnet/app/cli.py
[^tools]: src/decompnet/app/tools.py
