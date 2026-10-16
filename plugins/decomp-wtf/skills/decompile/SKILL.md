---
name: decompile
description: Run decomp.wtf for the user — install it, import a game they own, open the live view, start helper agents, report progress, export the rebuilt project. Use when the user asks to decompile, decomp or reverse engineer a game (ROM, disc image, ISO, executable), to rebuild its source for a port or mod, or to continue a public decomp (N64, PlayStation 1/2, GameCube, Wii, GBA, Game Boy/Color, DS).
---

# Running decomp.wtf for the user

decomp.wtf runs on the user's computer: the game's original compiler, a judge that compiles C and compares it with
the game byte for byte, free matches from machine drafts and twins, helper agents and a live view. Its tools come
from the `decomp` MCP server and may be deferred: search for them (e.g. ToolSearch `decomp import_game`) before
deciding they're missing.

The project is archived by its author (no more releases; the last is 0.10.6) because much of the decompilation
community objects to AI-made decomps. Work only on games the user owns, never publish or share the
game's code, and follow each public project's rules on AI contributions.

## Steps

1. **Installed?** If the tools are missing, ask before installing. Linux: `curl -fsSL https://decomp.wtf/install.sh | bash`;
   Windows (PowerShell): `irm https://decomp.wtf/install.ps1 | iex`; macOS (experimental): the one-line installer
   refuses macOS — run from a checkout with uv and Docker Desktop (`git clone https://github.com/i12bp8/decomp.wtf &&
   cd decomp.wtf && uv run decomp`), then `uv run decomp connect claude` in the checkout to register the MCP server
   (a checkout puts no `decomp` command on PATH). Then restart the session so the tools load.
2. **The game.** Ask the user to confirm the file is their own legally obtained copy, then
   `import_game(path, own_copy=true)`. A long import returns a job id: poll `import_status(job)`. Existing projects:
   `list_projects()` then `open_project(project)`. No ROM? Ask the user to open Demo Quest 64 from the app's home screen, then `open_project()` it.
3. **Show it.** `open_gui()` opens the live view.
4. **Let the free work run first.** Import starts the autopilot (machine drafts and twins of matched code) when its
   setting is on; `run_autopilot()` starts it again. `progress()` shows it.
5. **Work.** To decompile functions yourself, follow the `match-loop` skill. For a big game,
   `start_agents(count)` (1-8 per call) adds helper sessions with an installed agent CLI; starting helpers also starts
   the permuter, which polishes near misses. Use a CLI `kind` (claude, codex, antigravity, opencode): `kind="api"`
   can't be configured from here. `stop_agents()` stops the helpers only.
6. **Deliver.** `export_project()` writes a normal decomp project that rebuilds the game byte for byte (ROM
   projects; a fork is already a source tree).

Report progress as matched functions and % of code. Point the user to the app window for what has no tool: stopping
the autopilot or permuter, the ★ priority list, project instructions, the compiler after import (`import_game` takes `compiler`), the
spending budget, the model each helper uses, deleting or sharing a project.

## Identity

Each chat session is one agent identity named by `DECOMP_AGENT_NAME`; a second live session with the same name gets
its own (`name_2`, …). A session idle for 10+ minutes can be taken over, claims included, by a new session with its
name: the old one then gets "invalid API token" and needs a restart.
