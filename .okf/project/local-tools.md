---
type: Tooling
title: local-tools helper (decomp_local.py)
description: The checkout-local script that drives a running decomp.wtf instance over its HTTP API without printing the session key.
tags: [local-tools, api, helper, gba, rescan]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: script
    resource: local-tools/decomp_local.py
    title: decomp_local.py
  - id: readme
    resource: README.md
    title: Refining a GBA function map
  - id: rescan
    resource: src/decompnet/app/rescan.py
    title: GBA cfg reconciliation
---

# local-tools helper (decomp_local.py)

`local-tools/decomp_local.py` is the only file in `local-tools/`. It is a standard-library Python script that talks to the running local app "without printing its session key"[^script].

## How it connects

- **Secret.** It reads the GUI secret from `<repo>/decomp-project/.decomp/server.json`. That is the `.decomp` state of a workspace at `decomp-project/` inside the checkout. See [configuration and state](/project/configuration-and-state.md)[^script].
- **Auth.** The secret is sent as the `decomp_live` cookie. POSTs also send `X-Decomp: 1`[^script].
- **Defaults.** It targets `http://127.0.0.1:6464` (`--port` changes this) and the project slug `yu-gi-oh-ddm` (`--project` changes this)[^script].

## Commands

| Command | Purpose |
| :-- | :-- |
| `list [--limit --min-insns --max-insns --max-vram]` | Open functions, smallest first |
| `detail <name>` | Function assembly and current draft |
| `try <name> <source.c>` | Compiles C and compares it with the ROM |
| `diff <name>` | Latest instruction diff |
| `stop-autopilot` | Stops background matching for the project |
| `status` | Project and autopilot status |
| `import <path> [--toolchain]` | Imports a local game file (confirming ownership) and prints the job id |
| `job <id>` | Shows a job |
| `rescan [--apply --revision R]` | Previews, or applies, GBA function changes from an edited `functions.cfg` |

All commands are from `decomp_local.py`[^script]. `follow_job` polls `/api/job` until the state is `done` or `error`. It never repeats the request[^script].

## GBA rescan workflow

1. Edit the project's `functions.cfg`.
2. Stop its agents and autopilot.
3. Run `rescan` to preview.
4. Review the report.
5. Run `rescan --apply --revision '<preview revision>'`.

An apply needs that preview's revision in the same server session. Changed targets return to open status, and their old history is archived in `rescan_archives.snapshot`[^readme][^rescan].

[^script]: local-tools/decomp_local.py
[^readme]: README.md
[^rescan]: src/decompnet/app/rescan.py
