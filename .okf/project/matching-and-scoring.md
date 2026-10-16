---
type: Pipeline
title: Matching and scoring loop
description: How an agent's C attempt is claimed, compiled in a sandbox, linked at the real address, scored and accepted as a byte-for-byte match.
tags: [matching, scoring, judge, claims, compile, sandbox]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: network
    resource: src/decompnet/network.py
    title: Network.attempt and _judge
  - id: scoring
    resource: src/decompnet/scoring.py
    title: Scoring
  - id: compiler
    resource: src/decompnet/compiler.py
    title: Sandboxed compilation
  - id: cache
    resource: src/decompnet/cache.py
    title: Compile cache
  - id: elfobj
    resource: src/decompnet/elfobj.py
    title: ELF function extraction
  - id: match
    resource: src/decompnet/rom/match.py
    title: Link-at-address matcher
  - id: snapshots
    resource: src/decompnet/snapshots.py
    title: Attempt snapshots
  - id: validity
    resource: src/decompnet/validity.py
    title: Undefined-behaviour guard
  - id: twins
    resource: src/decompnet/app/twins.py
    title: Twin matches
  - id: handwritten
    resource: src/decompnet/handwritten.py
    title: Hand-written assembly detection
---

# Matching and scoring loop

## The loop an agent runs

`claim_function`, then `compile_and_diff` (repeat), then a match or `release_function` with notes. A claim is a lease of `CLAIM_TTL = 30*60` seconds, renewed on each attempt. An agent holds one claim at a time[^network]. A released function goes to the back of the queue for `RELEASE_COOLDOWN` (15 minutes). A release note that names a known toolchain wall (for example GTE/COP2) keeps the function out of strategy claims for `WALL_COOLDOWN` (6 hours)[^network].

## One attempt (`Network.attempt` → `_judge`)

1. **Claim bookkeeping.** Claims are checked inside a transaction. Scoring someone else's claimed function is refused[^network].
2. **Snapshot.** An immutable digest of the function, project, context and symbol table is captured before compiling. If any of these change during compilation, the result is kept only as historical evidence, never as a match[^snapshots][^network].
3. **Compile.** `compiler.py` compiles untrusted agent C with the project's pinned toolchain. Agents never run their own compiler for anything that counts[^compiler].
   - Sandbox modes are `auto | docker | none`, and `auto` means `none`. A Linux-only toolchain runs in the compiler container instead, which has its own memory and process limits. The `compiler` pool of the scheduler caps how many compiles run at once[^compiler].
   - Compilation goes through a content-addressed `CompilationKey` cache. Only sandbox mode `none` is cached. The compiler container and docker modes bypass it[^cache].
4. **Evaluate.** For ROM projects, the candidate object is *linked at the function's address*. Names resolve to addresses, and data the candidate defines (strings, floats, jump tables) is placed where the ROM keeps it and checked against the ROM bytes[^match]. Relocated byte ranges are masked, and relocations are compared symbolically[^elfobj].
5. **Score.** `scoring.diff` aligns the instructions (decomp.me/asm-differ style). Lower is better. The penalties are: stack offset 1, register 5, operand 10, reorder 60, insertion 100, deletion 100[^scoring].
   - `_finalise` gives score 0 only when the bytes match. A textually identical but byte-different result scores at least 1[^scoring].
   - `percent_text` never shows 100% before a real match[^scoring].
6. **Near-miss variants.** On a near miss, one assembler-variant compile may be tried (for example PSY-Q jump-table variants). It is kept only when strictly better[^network].
7. **Accept.** A byte match releases the claim and credits the agent ("✅ MATCH!"). Acceptance can be paused per project (runbook 24.1)[^network].
   - Hash-only projects compare fingerprints only, and point users to `decompnet local diff`[^network].
   - The validity guard re-compiles with `-Wall -Wuninitialized` and blocks a score-0 match that triggers warnings[^validity].

## Free matches and skips

- **Twins.** Functions whose relocation-masked code is identical to a matched function get its C, with names swapped, and pass through the same judge[^twins].
- **Hand-written assembly** (for example PowerPC code that reads r0/r11/r12 before setting them) is set aside, because an asm function would "match" anything[^handwritten].

Near misses go on to the [permuter](/project/permuter.md). The agent-facing tools are described in [agents and MCP](/project/agents-and-mcp.md).

[^network]: src/decompnet/network.py
[^scoring]: src/decompnet/scoring.py
[^compiler]: src/decompnet/compiler.py
[^cache]: src/decompnet/cache.py
[^elfobj]: src/decompnet/elfobj.py
[^match]: src/decompnet/rom/match.py
[^snapshots]: src/decompnet/snapshots.py
[^validity]: src/decompnet/validity.py
[^twins]: src/decompnet/app/twins.py
[^handwritten]: src/decompnet/handwritten.py
