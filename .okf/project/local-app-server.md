---
type: Module
title: Local app server
description: The local decomp.wtf server that serves the browser GUI, its JSON API and the agents' MCP endpoint, and how it is secured.
tags: [server, gui, api, mcp, security, uvicorn]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: appserver
    resource: src/decompnet/app/server.py
    title: Local server
  - id: appcli
    resource: src/decompnet/app/cli.py
    title: decomp run()
  - id: updates
    resource: src/decompnet/app/updates.py
    title: Self-update
---

# Local app server

## Startup (`decomp`, `app/cli.py: run`)

1. The workspace is `DECOMP_HOME` (default `~/Decomp`). Internal state lives in `<home>/.decomp/`[^appcli]. See [configuration and state](/project/configuration-and-state.md).
2. If `.decomp/server.json` points at a running app of the same or a newer version, that app is reused. An older one is asked to quit (`POST /api/quit`)[^appcli].
3. A socket is bound to the first free port from 6464. The bind address is 127.0.0.1 unless `DECOMP_BIND` sets another. A per-launch secret is reused from the previous `server.json` so that open tabs survive restarts[^appcli].
4. `RomNetwork(<home>/.decomp/decomp.db, home)` is created. `server.build()` returns the MCP server, the agent manager and the transport security settings. The MCP app is served by uvicorn, with an 80 MiB request body limit[^appcli].

## What it serves (`app/server.py`)

- **Binding.** It listens on 127.0.0.1 only[^appserver].
- **GUI auth.** The GUI needs the per-launch secret, handed to the browser as `?k=` and then kept in the `decomp_live` cookie[^appserver].
- **Agent auth.** Agents need their own bearer token[^appserver].
- **Host headers** are checked on every request, which blocks DNS rebinding[^appserver].
- **Endpoints.**
  - `/mcp`: the streamable-HTTP MCP endpoint for agents (see [agents and MCP](/project/agents-and-mcp.md))[^appserver]
  - `/health`: reports `{"app": "decomp-wtf", "version"}`[^appcli]
  - `/api/*`: GUI routes including import, projects, map, function, diff, try, live, autopilot, agents start/stop/install/token, fork (`/api/fork`, `/api/fork/setup`, `/api/fork/policy`, ...), collab start/join/stop, toolchain, settings, export, rescan, update, log, quit and job[^appserver]
- **Static GUI.** Served from `app/static/` (`index.html`, `app.js`, `app.css`)[^appserver].
- **Logging.** Long jobs are tracked by `Jobs` and logged to `.decomp/logs/decomp.log`. Settings live in `.decomp/settings.json`[^appserver].

## Updates

`app/updates.py` installs a newer version with `uv tool install --force`, then runs its `decomp setup`, then `exec`s the new version on the same port and browser session[^updates]. Updates come from either a "release" channel or a "branch" channel, as recorded by the installer in `install.json`[^updates].

[^appserver]: src/decompnet/app/server.py
[^appcli]: src/decompnet/app/cli.py
[^updates]: src/decompnet/app/updates.py
