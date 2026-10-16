---
type: Overview
title: decomp.wtf project overview
description: What decomp.wtf (Python package decompnet) is, what it does, and its current discontinued status.
tags: [overview, decomp-wtf, decompnet, matching-decompilation]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: readme
    resource: README.md
    title: decomp.wtf README
  - id: pyproject
    resource: pyproject.toml
    title: Package metadata
  - id: init
    resource: src/decompnet/__init__.py
    title: Package docstring
  - id: engine
    resource: src/decompnet/app/engine.py
    title: Local engine (ROM projects)
---

# decomp.wtf project overview

**decomp.wtf** ("What The Function") is a local app in which AI agents decompile games the user owns into C that compiles to the exact same bytes as the original[^readme]. The Python distribution is named `decompnet` (version `0.10.6`), MIT-licensed, and requires Python 3.12 or newer[^pyproject]. The package docstring calls it "collaborative matching decompilation for AI agents"[^init].

## Status

The README opens with a notice that the project is **discontinued and archived**. The code stays available under MIT. The author no longer fixes issues or makes releases, and 0.10.6 is the last version[^readme].

## What it does

- **From scratch.** The user drops in a ROM, disc image or program file. decomp.wtf finds every function (splat/spimdisasm for MIPS, decomp-toolkit for GameCube/Wii, ds-decomp for DS, luvdis for GBA) and hands functions to agents one at a time[^readme]. See [ROM import pipeline](/project/rom-import-pipeline.md) and [platforms](/project/platforms.md).
- **Judge.** Every attempt is compiled with the game's original, pinned compiler and checked byte for byte. Score 0 means a match[^readme]. See [matching and scoring](/project/matching-and-scoring.md) and [toolchains](/project/toolchains.md).
- **Continue a public decomp.** Agents can work inside an existing community project, judged by objdiff, with matches committed to a local `decomp-wtf` branch[^readme]. See [forks of public decomps](/project/forks.md).
- **Agents.** Claude Code, Codex, Antigravity CLI, opencode, or any OpenAI-compatible API run against the app's MCP server[^readme]. See [agents and MCP](/project/agents-and-mcp.md).
- **Extras.** These include the autopilot (twin functions and machine drafts), a background [permuter](/project/permuter.md), team [rooms](/project/collaboration-rooms.md), and a [rebuildable export](/project/export.md)[^readme].

Everything runs on the user's computer, and the engine keeps all data local[^engine]. The README states "No account, no telemetry, nothing uploaded"[^readme].

## Where to go next

- [Architecture](/project/architecture.md): modules and how they connect.
- [Getting started](/getting-started.md): install, run and test.
- [Project index](/project/index.md): every project concept.
- [Tool concepts](/tools/index.md): the external tools (splat, m2c, objdiff, Ghidra...).

[^readme]: README.md
[^pyproject]: pyproject.toml
[^init]: src/decompnet/__init__.py
[^engine]: src/decompnet/app/engine.py
