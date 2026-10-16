---
type: CLI
title: Command-line interfaces (decomp and decompnet)
description: Every subcommand of the two console scripts, decomp (the local app) and decompnet (the coordinator and toolchain manager).
tags: [cli, decomp, decompnet, commands]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: pyproject
    resource: pyproject.toml
    title: project.scripts
  - id: appcli
    resource: src/decompnet/app/cli.py
    title: decomp CLI
  - id: cli
    resource: src/decompnet/cli.py
    title: decompnet CLI
  - id: service
    resource: src/decompnet/app/service.py
    title: Background service
---

# Command-line interfaces (decomp and decompnet)

`pyproject.toml` declares two scripts: `decomp = decompnet.app.cli:main` and `decompnet = decompnet.cli:main`[^pyproject].

## `decomp`: the local app

With no subcommand, `decomp` starts the app, or brings an already running one to the front, and opens it in the browser[^appcli]. Global options are `--port` (default `DECOMP_PORT` or 6464) and `--no-browser`.

| Command | What it does |
| :-- | :-- |
| *(none)* | Runs the server (see [local app server](/project/local-app-server.md)). An older running version is stopped and replaced. |
| `setup` | Installs `gcc-2.7.2-kmc`, `m2c` and `objdiff-cli`, and lists the agent CLIs it finds. |
| `stop` | POSTs `/api/quit` to the running app. |
| `shortcut` | Adds decomp.wtf to the apps menu or desktop. |
| `url` | Prints the signed-in address, starting the app if needed. |
| `update [--check]` | Installs the newest version. A running app restarts into it. |
| `connect [agents...] [--list]` | Registers `decomp mcp` with claude, codex, antigravity (agy) or opencode. |
| `share <project> [--address --port --public --approve]` | Opens a team room for a project. |
| `host <game> [--own-copy --address --port --public --approve --service]` | Imports a game and opens a room. Default room port is 6481. |
| `join <link> <game> --name N [--own-copy]` | Joins a room with your own copy. |
| `service install\|remove\|status` | Runs the app in the background. |
| `mcp [--name]` | Starts the stdio MCP bridge for the user's own agent. |
| `install-info` | Hidden. The installers use it to record the update channel. |

All rows are from `app/cli.py`[^appcli]. The background service is a systemd user service on Linux, a launchd agent on macOS, and a Run-key startup app on Windows, each running `decomp --no-browser`[^service].

## `decompnet`: coordinator and tools

The global option is `--db` (default `$DECOMPNET_DB` or `decompnet.db`)[^cli].

| Command | What it does |
| :-- | :-- |
| `serve [--host --port 8765 --public-url --open-registration --api-url]` | Runs the open-network coordinator: MCP plus the public API. |
| `import <path>` | Imports or refreshes a project directory that contains a `project.toml`. |
| `config <path>` | Validates `project.toml` and prints its effective configuration. |
| `agent add <name> [--owner --url]` / `agent revoke <name> [--erase]` | Mints or revokes agent tokens. |
| `project remove <slug> --yes` | Takedown: deletes a project. |
| `function reset <project> <name>` | Deletes a function's submissions and reopens it. |
| `note list <project>` / `note delete <id>` | Note moderation. |
| `toolchain list` / `install <id> [--accept-license --force]` / `path <id>` | Manages pinned toolchains (see [toolchains](/project/toolchains.md)). |
| `worker [--server --token --minutes --jobs]` | Donates permuter compute (see [permuter](/project/permuter.md)). |
| `local asm <obj> <fn>` / `local diff <obj> <fn> <src> --toolchain T` | Hash-only projects: diffs against your own object file. |
| `upstream <project> <repo>` | Commits matched functions to a git repo, one commit per match. |
| `status` | Prints progress and the leaderboard. |
| `export <project> <out>` | Writes matched sources plus `context.h`. |

All rows are from `cli.py`[^cli]. See [coordinator network](/project/coordinator-network.md).

[^pyproject]: pyproject.toml
[^appcli]: src/decompnet/app/cli.py
[^cli]: src/decompnet/cli.py
[^service]: src/decompnet/app/service.py
