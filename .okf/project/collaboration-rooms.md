---
type: Module
title: Collaboration rooms (decompile together)
description: How a host's decomp.wtf runs a room where team members, each with their own copy of the game, share claims, attempts, matches and notes.
tags: [collaboration, rooms, collab, team, tunnel]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: collab
    resource: src/decompnet/app/collab.py
    title: Rooms for ROM projects
  - id: forkroom
    resource: src/decompnet/app/forkroom.py
    title: Rooms for git source-tree projects
  - id: appcli
    resource: src/decompnet/app/cli.py
    title: share/host/join commands
  - id: toml
    resource: src/decompnet/toolchains.toml
    title: cloudflared entry
  - id: readme
    resource: README.md
    title: Decompile together
---

# Collaboration rooms (decompile together)

## Model

The host's decomp.wtf is the hub. It serves a small, separate Starlette app (`host_app()`) with only `/collab/*` routes: never the GUI or the agents' MCP endpoint[^collab].

- **Routes:** `hello`, `join`, `state`, `whoami`, `claim`, `attempt`, `best`, `release`, `context`, `rename`, `split`, `note`, `leave`, `edit`, `split_file`, `bundle`[^collab].
- **Joining.** A guest joins with the invite link *and their own copy*. The ROM's SHA-1 must equal the host's, checked on both sides[^collab].
- **What travels.** No ROM bytes or assembly ever cross the wire. Only the work does: claims, C attempts, matches, names, struct/prototype edits and notes[^collab].
- **Verification.** The host re-compiles every attempt it accepts against its own ROM[^collab].
- **Guest side.** A `Link` object, which `RomNetwork` consults for every collaborative action[^collab].

## Git source-tree projects (`app/forkroom.py`)

GameCube, Wii and DS scratch projects, and forks, use the host's tree as the truth[^forkroom]:

- Members compile and diff on their own computers.
- A member's match is sent to the host, which builds it, checks it and commits it.
- Edits of shared files are made by the host.
- Members follow the host's commits as git bundles. These contain only tracked files, never the game or the build[^forkroom].

## Reaching the room

By default the room is reached through a free Cloudflare quick tunnel (the `cloudflared` tool, no account)[^toml]. `--address` (for example `http://my-server:6481`, port 6481 by default) uses an address the host controls instead. Rooms are private (invite link) unless `--public`, and `--approve` makes the host let each member in[^appcli].

The commands are `decomp share`, `decomp host` and `decomp join` (see [CLI](/project/cli.md))[^appcli]. Members compile their own attempts, so the host compiles only to verify matches[^readme].

[^collab]: src/decompnet/app/collab.py
[^forkroom]: src/decompnet/app/forkroom.py
[^appcli]: src/decompnet/app/cli.py
[^toml]: src/decompnet/toolchains.toml
[^readme]: README.md
