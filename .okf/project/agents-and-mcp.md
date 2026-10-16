---
type: Interface
title: Agents and the MCP interface
description: Which AI agents decomp.wtf runs, how they connect over MCP, and the full list of tools they get.
tags: [agents, mcp, tools, claude-code, codex, opencode, antigravity]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: tools
    resource: src/decompnet/app/tools.py
    title: Work tools and guides
  - id: chat
    resource: src/decompnet/app/chat.py
    title: Tools for the user's own agent
  - id: agents
    resource: src/decompnet/app/agents.py
    title: Agent runner
  - id: bridge
    resource: src/decompnet/app/bridge.py
    title: decomp mcp bridge
  - id: connect
    resource: src/decompnet/app/connect.py
    title: decomp connect
  - id: plugin
    resource: plugins/decomp-wtf/.mcp.json
    title: Claude Code plugin MCP config
  - id: skill
    resource: plugins/decomp-wtf/skills/decompile/SKILL.md
    title: decompile skill
  - id: readme
    resource: README.md
    title: Agents section
---

# Agents and the MCP interface

## Agent kinds

`app/agents.py` defines `KINDS`: `claude` (Claude Code), `codex` (Codex CLI), `antigravity` (Antigravity CLI, `agy`), `opencode`, and `api`. The `api` kind is a built-in tool loop for any OpenAI-compatible endpoint: DeepSeek, OpenRouter, Ollama and similar[^agents].

- **How CLI agents run.** They run headless in a private folder, connected only to the app's MCP endpoint. Claude Code, for example, is launched with `--strict-mcp-config --allowedTools mcp__decomp`. Each session handles a few functions and exits, and the runner starts a fresh one. The prompt goes in on stdin[^agents].
- **Access.** Agents get only decomp.wtf's tools: no shell, and no file access beyond a fork's own source tree[^readme].

## The user's own agent

`decomp mcp` (`app/bridge.py`) is a stdio MCP server[^bridge]. When needed, it:

- starts the app in the background
- gives the agent its own identity (named by `--name` or `DECOMP_AGENT_NAME`)
- passes the app's tools through

stdout is the MCP channel, and everything else goes to stderr[^bridge].

`decomp connect` registers it with Claude Code, Codex, Antigravity CLI and opencode[^connect]. The Claude Code plugin at `plugins/decomp-wtf` does the same through `.mcp.json` (`decomp mcp`, `DECOMP_AGENT_NAME=claude-code`) and adds a `decompile` skill[^plugin][^skill].

## Work tools (`app/tools.py`)

The core loop, available in every project[^tools]:

- `project_info`
- `claim_function(function, strategy, count)`
- `get_function`
- `compile_and_diff(function, source)`: several comma-separated names judge one batch
- `release_function(function, notes)`
- `read_data`
- `list_functions`
- `add_note` and `search_notes`
- `find_similar`
- `guide(topic)`

ROM projects only: `get_context`, `edit_context`, `rename_symbol`, `split_function`, `rescan_gba`.

Fork projects only: `read_file`, `search_code`, `edit_file`, `file_status`, `split_file`.

`guide(topic)` serves the Markdown files in `app/guide/`. Topics include workflow, mips, gcc-kmc, ido, m2c, data, structs, n64, naming, fork, objdiff, ppc, mwcc, cpp, psx, ps2, arm, agbcc, gb, x86, aarch64 and similarity. `PLATFORM_GUIDES` lists which ones to read first on each platform[^tools].

## Chat tools (`app/chat.py`)

Only the user's own agent gets these[^chat]:

- `list_projects`, `open_project`
- `import_game(path, own_copy, compiler)`, `import_status`
- `open_gui`
- `start_agents`, `stop_agents`
- `progress`
- `run_autopilot`
- `analyze_target`: [Ghidra](/project/ghidra-integration.md) evidence
- `export_project`

Helper agents the app starts itself get only the work tools, so a helper can't start more helpers[^chat].

See [matching and scoring](/project/matching-and-scoring.md) for what `compile_and_diff` does, and [local app server](/project/local-app-server.md) for where `/mcp` is served.

[^tools]: src/decompnet/app/tools.py
[^chat]: src/decompnet/app/chat.py
[^agents]: src/decompnet/app/agents.py
[^bridge]: src/decompnet/app/bridge.py
[^connect]: src/decompnet/app/connect.py
[^plugin]: plugins/decomp-wtf/.mcp.json
[^skill]: plugins/decomp-wtf/skills/decompile/SKILL.md
[^readme]: README.md
