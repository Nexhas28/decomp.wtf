---
type: Module
title: Permuter (background and volunteer)
description: How decomp.wtf rewrites near-miss C in the background with syntax-aware mutations and a guided search, and how volunteers donate compute.
tags: [permuter, search, near-miss, mutations]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: apppermute
    resource: src/decompnet/app/permute.py
    title: Background permuter in the app
  - id: permuter
    resource: src/decompnet/permuter.py
    title: Mutations and decompnet worker
  - id: search
    resource: src/decompnet/search/__init__.py
    title: Guided search package
  - id: run
    resource: src/decompnet/search/run.py
    title: SearchRun
  - id: routing
    resource: src/decompnet/search/routing.py
    title: Mismatch routing
  - id: external
    resource: src/decompnet/search/external.py
    title: Optional decomp-permuter adapter
  - id: jobs
    resource: src/decompnet/jobs.py
    title: Coordinator-side jobs
  - id: project
    resource: src/decompnet/project.py
    title: CONFIG_SCHEMA search block
---

# Permuter (background and volunteer)

## In the app (`app/permute.py`)

The permuter runs on spare CPU over near misses that agents or autopilot drafts left behind. A function counts as a near miss when `best_score <= n_insns * 100 * NEAR`, with `NEAR = 0.06`[^apppermute]. The function must also:

- be open and unclaimed
- have at most 400 instructions
- still have search budget under the current policy

Candidates are tried closest first[^apppermute].

Each turn holds a function for `SECONDS = 60`, so an agent that wants it gets it next. A function gets at most `MAX_TURNS = 4` turns before it is left to agents. Every improvement becomes the function's best attempt[^apppermute]. Runs checkpoint so they can resume, with `CHECKPOINT_LIMIT = 64`[^apppermute].

## Mutations (`permuter.py`)

Mutations are syntax-aware and labelled, in two categories[^permuter]:

- **restricted**: equivalence rewrites, applied only when their preconditions hold (no volatile accesses, unknown calls, overlapping writes, ...)
- **exploratory**: rewrites that may change meaning, which the judge then filters

## Guided search (`search/`)

- `routing.py` routes the structured mismatch counts (register, stack_offset, operand, insertion, deletion, reorder) to operator families. It only re-weights them and never excludes any[^routing].
- `portfolio.py` keeps a bounded, diverse beam.
- `dedup.py` reuses evaluations by source digest and object digest.
- `run.py` seeds each run deterministically from (function id, turn, policy epoch), so a run is reproducible and resumable[^run].

The byte judge still decides every acceptance[^search].

**The guided search is opt-in.** It runs only with `DECOMPNET_GUIDED_SEARCH=1`, which selects policy `guided_v1`. Without it, the permuter runs the single-trajectory policy `single_v1`[^apppermute][^project]. See [configuration and state](/project/configuration-and-state.md).

**Upstream decomp-permuter** is optional. It is not vendored, and its scores are advisory: every candidate is re-verified through the coordinator[^external].

## Volunteer workers

`decompnet worker` fetches a near-matching function from an open-network coordinator, searches locally with the same pinned toolchain and scorer, and submits improvements[^permuter]. The coordinator re-compiles everything, so a worker only donates search time. Only projects with a pinned toolchain and full targets take part[^jobs]. See [coordinator network](/project/coordinator-network.md).

[^apppermute]: src/decompnet/app/permute.py
[^permuter]: src/decompnet/permuter.py
[^search]: src/decompnet/search/__init__.py
[^run]: src/decompnet/search/run.py
[^routing]: src/decompnet/search/routing.py
[^external]: src/decompnet/search/external.py
[^jobs]: src/decompnet/jobs.py
[^project]: src/decompnet/project.py
