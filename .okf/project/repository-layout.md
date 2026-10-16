---
type: Reference
title: Repository layout
description: The top-level folders of the decomp.wtf checkout (src, tests, projects, benchmarks, plugins, site, local-tools) and what each holds.
tags: [repository, layout, projects, benchmarks, plugins, site]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: readme
    resource: README.md
    title: Repository layout table
  - id: benchreadme
    resource: benchmarks/README.md
    title: Benchmarks and fixtures
  - id: demon64
    resource: projects/demo-n64/project.toml
    title: Demo Quest 64 project
  - id: zlib
    resource: projects/zlib-n64/project.toml
    title: zlib for N64 project
  - id: plugin
    resource: plugins/decomp-wtf/.claude-plugin/plugin.json
    title: Claude Code plugin manifest
  - id: wrangler
    resource: site/wrangler.jsonc
    title: Website deployment config
  - id: compose
    resource: docker-compose.yml
    title: Docker Compose file
---

# Repository layout

| Path | What it holds |
| :-- | :-- |
| `src/decompnet/` | The Python package. See [architecture](/project/architecture.md). |
| `tests/` | The pytest suite. See [testing](/project/testing.md). |
| `projects/` | Demo and fixture projects (below). |
| `benchmarks/` | The measurement harness and plan documents (below). |
| `plugins/decomp-wtf/` | The Claude Code plugin. See [agents and MCP](/project/agents-and-mcp.md). |
| `site/`, `site-redirect/` | The decomp.wtf website and installers. |
| `local-tools/` | `decomp_local.py`. See [local-tools](/project/local-tools.md). |
| `docker-compose.yml` | Runs the whole app in Docker[^compose]. See [host environments](/project/host-environments.md). |
| `pyproject.toml`, `uv.lock`, `LICENSE` | Package metadata (uv build backend) and the MIT license. |

## `projects/`

- `demo-rom/` builds **Demo Quest 64**, the N64 ROM that ships with the app. `tests/test_rom.py` proves the matcher on it. Rebuild it with `uv run python projects/demo-rom/build.py`, which needs `gcc-2.7.2-kmc` and `ld.lld`[^readme].
- `demo-psx/` is the same game as a PlayStation executable, for `tests/test_psx.py`. `demo-fork/` is a small community-shaped decomp (`objdiff.json`, ninja) for `tests/test_forks.py`[^readme].
- `demo-ps2`, `demo-gba`, `demo-gc` and `demo-ds` hold per-platform demo builds (`build.py`, crt0, a runtime and a linker script).
- `demo/` (x86-64 toy) and `demo-n64/` are coordinator projects with `project.toml`[^demon64].
- `zlib-n64/` is zlib 1.1.4 built for N64 with KMC GCC 2.7.2, under the Zlib license[^zlib].

## `benchmarks/`

This is the "Phase 0" baseline and measurement harness[^benchreadme]:

- `baseline-manifest.json`, `baseline-pytest.txt`
- `fixtures/`
- `harness/`: `replay.py`, `ablate.py`, `draft_compare.py`, `holdout_recall.py`, `stats.py`, `rea_discover.py`
- plan documents: `ROADMAP.md`, `DECISIONS.md`, `FEATURE-FLAGS.md`, `RUNBOOKS.md`, `FINAL-REPORT.md`, ...

`results/<run-id>/` is disposable output[^benchreadme].

## `plugins/decomp-wtf/`

The plugin manifest (version `0.9.8`, MIT), an `.mcp.json` that runs `decomp mcp`, and a `decompile` skill[^plugin].

## `site/`

The website is deployed as Cloudflare Workers static assets from `site/public` (`wrangler.jsonc`, custom domain `decomp.wtf`)[^wrangler]. `site/public` also holds the installers: `install.sh`, `install.ps1`, `linux-base.sh` and the app `Dockerfile`. `site/tools/build_sim.py` builds the simulation data[^readme].

[^readme]: README.md
[^benchreadme]: benchmarks/README.md
[^demon64]: projects/demo-n64/project.toml
[^zlib]: projects/zlib-n64/project.toml
[^plugin]: plugins/decomp-wtf/.claude-plugin/plugin.json
[^wrangler]: site/wrangler.jsonc
[^compose]: docker-compose.yml
