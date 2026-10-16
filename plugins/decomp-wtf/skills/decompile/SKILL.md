---
name: decompile
description: Run decomp.wtf for the user: install, import a game they own, open the live view, start helper agents, report progress, export. Read when the user asks to decompile or reverse engineer a game (ROM, disc image, executable) or continue a public decomp.
---

# Running decomp.wtf for the user

Tools come from the `decomp` MCP server and may be deferred: search (ToolSearch `decomp import_game`) before
deciding they're missing. Work only on games the user owns; never publish the game's code; follow public
projects' rules on AI contributions.

1. **Installed?** If not, ask first. Mac app (macOS 14+) needing [uv](https://docs.astral.sh/uv/), a clone of
   `https://github.com/Nexhas28/decomp.wtf` (releases page has the app), sometimes Docker Desktop. Then
   `uv run decomp connect claude` in the clone and restart the session. Other systems: say so, install nothing.
2. **Game:** ask the user to confirm it's their own legal copy, then `import_game(path, own_copy=true)`; a long
   import gives a job id for `import_status(job)`. Existing: `list_projects()`, `open_project(project)`.
   No ROM: have them open Demo Quest 64 from the app's home screen.
3. `open_gui()` shows the live view. Import starts the free autopilot; `run_autopilot()` restarts it.
4. **Work:** follow `match-loop`. For big games `start_agents(count)` (1-8, a CLI `kind`: claude, codex,
   antigravity, opencode) adds helpers and the permuter. `stop_agents()` stops helpers only.
5. **Deliver:** `export_project()` rebuilds the game byte for byte.

Report matched functions and % of code. Things only the app window does: stop autopilot/permuter, ★ priorities,
project instructions, compiler choice, budget, per-helper model, delete/share project.
Each session is one agent identity (`DECOMP_AGENT_NAME`); idle 10+ minutes it can be taken over.
