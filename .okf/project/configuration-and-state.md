---
type: Configuration
title: Configuration, state and environment variables
description: Where decomp.wtf keeps its state (the workspace and its .decomp folder), the project.toml format, and the environment variables the code reads.
tags: [configuration, state, environment, project-toml, decomp-home]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: appcli
    resource: src/decompnet/app/cli.py
    title: home_dir, run, server.json
  - id: appserver
    resource: src/decompnet/app/server.py
    title: Logs and settings paths
  - id: engine
    resource: src/decompnet/app/engine.py
    title: Lockdown settings and uploads
  - id: forkengine
    resource: src/decompnet/app/forkengine.py
    title: catalog.json
  - id: project
    resource: src/decompnet/project.py
    title: project.toml and CONFIG_SCHEMA
  - id: demon64
    resource: projects/demo-n64/project.toml
    title: Example project.toml
  - id: compiler
    resource: src/decompnet/compiler.py
    title: Compile env vars
  - id: telemetry
    resource: src/decompnet/telemetry.py
    title: DECOMP_TELEMETRY
  - id: toolchains
    resource: src/decompnet/toolchains.py
    title: Toolchain root
  - id: cli
    resource: src/decompnet/cli.py
    title: decompnet env vars
  - id: hostenv
    resource: src/decompnet/hostenv.py
    title: Runtime detection
---

# Configuration, state and environment variables

## Workspace and `.decomp/`

The workspace (projects folder) is `DECOMP_HOME`, defaulting to `~/Decomp`[^appcli]. Each project lives in `<home>/<slug>/`, and the app's internal state lives in `<home>/.decomp/`:

| Path | What it is |
| :-- | :-- |
| `.decomp/decomp.db` | The SQLite database the `RomNetwork` uses[^appcli] |
| `.decomp/server.json` | `{port, secret, pid}` of the running app, chmod 600[^appcli] |
| `.decomp/settings.json` | App settings, for example `autopilot_after_match`, `budget_usd`, `running_agents`, and the fleet `lockdown` / `allowed_agents`[^appserver][^engine] |
| `.decomp/logs/decomp.log` | The app's job and error log[^appserver] |
| `.decomp/app.log` | stdout/stderr when started without a console (pythonw)[^appcli] |
| `.decomp/uploads/` | Uploaded GameCube/Wii/DS game files before scratch setup[^engine] |
| `.decomp/catalog.json` | The cached decomp.dev catalog[^forkengine] |

The fleet lockdown (`"lockdown": true` in `settings.json`) refuses to create new agent identities, except names in `allowed_agents`. It applies only to the instance that owns `<home>/.decomp/decomp.db`. `DECOMP_LOCKDOWN=1` or `=0` forces it on or off[^engine].

## `project.toml` (decompnet coordinator projects)

A coordinator project directory has a `project.toml` with these sections[^demon64]:

- `name`, `slug`, `description`
- `[legal]`: `basis`, `license`, `notes`
- `[compiler]`: a `toolchain` id or a `cmd` template, plus `timeout`, `include_dirs` and `sandbox`
- `[target]`: `objects`, `build_from`
- `[context]`: `file`, `units`

`decompnet config <path>` validates it[^project]. `project.py`'s `CONFIG_SCHEMA` also governs the optional `[matching]`, `[search]`, `[analysis]` and `[rea]` blocks[^project]:

- `search.policy` defaults to `single_v1`.
- `analysis.enabled` and `rea.enabled` default to `false`.
- An environment override, where one exists, wins over the project value.

ROM projects imported in the app store their config as JSON in the database instead. See [ROM import](/project/rom-import-pipeline.md).

## Environment variables (read in code)

| Variable | Effect |
| :-- | :-- |
| `DECOMP_HOME` | Workspace folder (default `~/Decomp`)[^appcli] |
| `DECOMP_PORT` | App port (default 6464)[^appcli] |
| `DECOMP_BIND` | Bind address (default 127.0.0.1, or 0.0.0.0 in Docker)[^appcli] |
| `DECOMP_AGENT_NAME` | Name for `decomp mcp`[^appcli] |
| `DECOMP_RUNTIME` | Forces `native`, `wsl` or `docker` detection[^hostenv] |
| `DECOMP_LOCKDOWN` | Forces the fleet lockdown[^engine] |
| `DECOMP_TELEMETRY=on` | Stage-level telemetry (off by default)[^telemetry] |
| `DECOMP_COMPILER_AS_LIMIT` | Compiler address-space cap in bytes (default 2 GiB, `0` = none)[^compiler] |
| `DECOMPNET_COMPILE_CACHE=0` | Disables the compile cache[^compiler] |
| `DECOMPNET_CACHE_DIR` | Compile cache location (default `<tmp>/decompnet-cache`)[^compiler] |
| `DECOMPNET_TOOLCHAINS` | Toolchain install root[^toolchains] |
| `DECOMPNET_DB` / `DECOMPNET_TOKEN` | `decompnet` database path / worker token[^cli] |
| `DECOMPNET_GUIDED_SEARCH=1` | Guided permuter search (`guided_v1`)[^project] |
| `DECOMPNET_STRUCTURAL_RERANK=1` | Structural rerank in search[^project] |
| `DECOMPNET_REA_PROVIDER=1` | Optional REA analysis provider[^project] |

Ghidra, WSL and Docker variables are covered in [Ghidra integration](/project/ghidra-integration.md) and [host environments](/project/host-environments.md).

[^appcli]: src/decompnet/app/cli.py
[^appserver]: src/decompnet/app/server.py
[^engine]: src/decompnet/app/engine.py
[^forkengine]: src/decompnet/app/forkengine.py
[^project]: src/decompnet/project.py
[^demon64]: projects/demo-n64/project.toml
[^compiler]: src/decompnet/compiler.py
[^telemetry]: src/decompnet/telemetry.py
[^toolchains]: src/decompnet/toolchains.py
[^cli]: src/decompnet/cli.py
[^hostenv]: src/decompnet/hostenv.py
