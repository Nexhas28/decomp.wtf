---
type: Architecture
title: decompnet architecture and module map
description: The packages and modules of src/decompnet and how the coordinator, local engine, ROM layer, forks and servers connect.
tags: [architecture, modules, decompnet, coordinator, engine]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: readme
    resource: README.md
    title: Repository layout table
  - id: network
    resource: src/decompnet/network.py
    title: The coordinator (Network)
  - id: engine
    resource: src/decompnet/app/engine.py
    title: Local engine (RomNetwork)
  - id: forkengine
    resource: src/decompnet/app/forkengine.py
    title: Fork engine
  - id: appserver
    resource: src/decompnet/app/server.py
    title: Local server
  - id: tools
    resource: src/decompnet/app/tools.py
    title: MCP tools
  - id: store
    resource: src/decompnet/store.py
    title: SQLite persistence
  - id: compiler
    resource: src/decompnet/compiler.py
    title: Sandboxed compilation
  - id: analysis
    resource: src/decompnet/analysis/__init__.py
    title: Analysis broker
  - id: retrieval
    resource: src/decompnet/retrieval/__init__.py
    title: Retrieval package
  - id: search
    resource: src/decompnet/search/__init__.py
    title: Guided search package
---

# decompnet architecture and module map

## Layers

1. **Coordination core: `network.py`.** The `Network` class decides who works on what, what counts as a match, and what everyone has learned. Its public methods are agent-facing actions that return plain text for an LLM to read[^network]. It holds claims with a 30-minute lease (`CLAIM_TTL`), a release cooldown, claim strategies (`easiest`, `finish`, `closest`, `hardest`, `random`, `priority`) and the single acceptance path `_judge`[^network]. Persistence is one SQLite file through `store.py`: "One file, one connection, one lock"[^store].
2. **Local engine: `app/engine.py`.** `RomNetwork` builds on `Network` and adds ROM projects. Targets come straight from the ROM, a symbol table lets agents rename without breaking matches, and there are tools for splitting functions and reading data[^engine].
3. **Fork engine: `app/forkengine.py`.** This adds projects that are git source trees: public decomps, plus GameCube, Wii and DS games started from scratch. objdiff judges them inside the project's own build[^forkengine]. See [forks](/project/forks.md).
4. **Servers.**
   - `app/server.py` is the local app: the GUI, its JSON API and the agents' MCP endpoint, on 127.0.0.1[^appserver]. See [local app server](/project/local-app-server.md).
   - `server.py` is the separate open-network coordinator. See [coordinator network](/project/coordinator-network.md).
5. **Agent surface: `app/tools.py`, `app/chat.py`, `app/agents.py`, `app/bridge.py`.** These hold the MCP tools and the runners for CLI and API agents[^tools]. See [agents and MCP](/project/agents-and-mcp.md).

## Supporting packages

| Package / module | Role |
| :-- | :-- |
| `rom/` | Reading ROMs and discs, splitting (splat, luvdis, mgbdis), symbol tables, and matching by linking at the real address. See [ROM import](/project/rom-import-pipeline.md). |
| `forks/` | decomp.dev catalog, project detection/setup, objdiff, C source editing, m2c/Ghidra drafts, and dtk-template splits[^readme]. |
| `compiler.py`, `cache.py` | Sandboxed compilation of untrusted agent C, through a content-addressed compile cache[^compiler]. |
| `scoring.py`, `elfobj.py` | Disassembly, instruction diff and score. See [matching and scoring](/project/matching-and-scoring.md). |
| `toolchains.py` + `toolchains.toml` | Pinned compiler registry and verified installs. See [toolchains](/project/toolchains.md). |
| `permuter.py`, `search/`, `app/permute.py` | Syntax-aware permutation of near misses[^search]. See [permuter](/project/permuter.md). |
| `analysis/` | Analysis broker over evidence providers: direct Ghidra, and an optional REA adapter[^analysis]. See [Ghidra integration](/project/ghidra-integration.md). |
| `retrieval/` | Optional Function ID and BSim retrieval. It is off by default and returns candidates only, never verdicts[^retrieval]. |
| `similarity.py`, `app/twins.py` | Look-alike functions and free matches from identical twins. |
| `hostenv.py`, `linuxbox.py`, `limits.py` | Where the app runs (always natively on the Mac), and how Linux-only compilers run in the compiler container. See [host environments](/project/host-environments.md). |
| `app/collab.py`, `app/forkroom.py` | Team rooms. See [collaboration rooms](/project/collaboration-rooms.md). |

## Data flow of one attempt

The agent calls `compile_and_diff` (MCP), which runs `tools.HANDLERS`, then `RomNetwork.attempt` / `Network.attempt`. That step takes the claim, captures a snapshot, compiles in the sandbox, and passes the result to `_judge`. `_judge` links and compares against the ROM target, scores, records the attempt, and reports either a match or a diff text[^tools][^network].

[^readme]: README.md
[^network]: src/decompnet/network.py
[^engine]: src/decompnet/app/engine.py
[^forkengine]: src/decompnet/app/forkengine.py
[^appserver]: src/decompnet/app/server.py
[^tools]: src/decompnet/app/tools.py
[^store]: src/decompnet/store.py
[^compiler]: src/decompnet/compiler.py
[^analysis]: src/decompnet/analysis/__init__.py
[^retrieval]: src/decompnet/retrieval/__init__.py
[^search]: src/decompnet/search/__init__.py
