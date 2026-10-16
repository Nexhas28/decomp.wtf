---
type: Runbook
title: Testing the project
description: How to run the decompnet test suite, how it is organised, the recorded baseline, and which tests depend on installed toolchains.
tags: [testing, pytest, runbook, baseline]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: conftest
    resource: tests/conftest.py
    title: Test configuration
  - id: testds
    resource: tests/test_ds.py
    title: DS tests and their skip gate
  - id: pyproject
    resource: pyproject.toml
    title: pytest settings and dev group
  - id: benchreadme
    resource: benchmarks/README.md
    title: Benchmark harness commands
  - id: benchbaseline
    resource: benchmarks/baseline-pytest.txt
    title: Older recorded pytest run
  - id: readme
    resource: README.md
    title: Development section
  - id: linuxbox
    resource: src/decompnet/linuxbox.py
    title: DECOMP_LINUXBOX hook
  - id: engine
    resource: src/decompnet/app/engine.py
    title: DECOMP_LOCKDOWN hook
---

# Testing the project

## Run it

```bash
.venv/bin/python -m pytest -q tests
```

Run this from the repository root. The README's equivalent is `uv sync` followed by `uv run pytest`[^readme]. `pytest>=9.1.1` is in the `dev` dependency group, and `pyproject.toml` filters one Starlette TestClient timeout warning[^pyproject]. The benchmarks README uses `PYTHONPATH=src .venv/bin/python -m pytest tests/ -q`[^benchreadme].

## Baseline

The operator-reported baseline for this checkout, recorded when this bundle was created, is **897 passed**. The only failures are in `tests/test_ds.py`, attributed to the DS toolchain not being installed.

`tests/test_ds.py` gates its tests with a module-level `pytestmark = pytest.mark.skipif(...)`. It skips when any of `mwccarm`, `dsd` or `objdiff-cli` is not installed, or when `wibo` is supported on the platform but not installed. Some tests also `pytest.skip("needs m2c")`[^testds].

`benchmarks/baseline-pytest.txt` is an **older, different** recorded run (17 failed, 107 passed, 115 skipped, 30 errors). Do not treat it as the current baseline[^benchbaseline].

## Layout

`tests/` holds 93 `test_*.py` modules, roughly one per feature area:

- platforms: `test_rom`, `test_psx`, `test_ps2`, `test_gba`, `test_gb`, `test_gc`, `test_ds`
- forks: `test_forks`, `test_dtksplits`, `test_fork_rooms`
- scoring and matching: `test_scoring`, `test_similarity`, `test_validity`
- toolchains: `test_toolchains`, `test_toolchain_paths`
- hosts: `test_linuxbox`, `test_hostenv`
- servers: `test_app_server`, `test_http`, `test_endpoints`
- permuter: `test_permute`, `test_permuter_ops`, `test_search`
- `tests/solutions.py`: helper solutions

`conftest.py` sets `DECOMP_UPDATES=off`, so apps started in tests never look for updates on the network[^conftest].

The demo projects in `projects/` are test fixtures. For example, `tests/test_rom.py` proves the matcher on `projects/demo-rom` (Demo Quest 64), and `tests/test_forks.py` uses `projects/demo-fork`[^readme]. See [repository layout](/project/repository-layout.md).

## Env hooks used by tests

These variables point code at test stand-ins:

- `DECOMP_LINUXBOX`: stand-in for `docker`[^linuxbox]
- `DECOMP_LOCKDOWN`: forces fleet lockdown on or off[^engine]

See [configuration and state](/project/configuration-and-state.md).

[^conftest]: tests/conftest.py
[^testds]: tests/test_ds.py
[^pyproject]: pyproject.toml
[^benchreadme]: benchmarks/README.md
[^benchbaseline]: benchmarks/baseline-pytest.txt
[^readme]: README.md
[^linuxbox]: src/decompnet/linuxbox.py
[^engine]: src/decompnet/app/engine.py
