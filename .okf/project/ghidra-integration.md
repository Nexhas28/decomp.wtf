---
type: Integration
title: Ghidra integration and the analysis broker
description: How decomp.wtf uses headless Ghidra for read-only evidence and drafts, and how the analysis broker governs optional providers.
tags: [ghidra, analysis, evidence, broker, rea, java]
generated:
  by: claude-code/claude-opus-5-5
  at: '2026-10-16T11:51:12Z'
sources:
  - id: ghidra
    resource: src/decompnet/forks/ghidra.py
    title: Ghidra evidence
  - id: chat
    resource: src/decompnet/app/chat.py
    title: analyze_target tool
  - id: toml
    resource: src/decompnet/toolchains.toml
    title: ghidra registry entry
  - id: broker
    resource: src/decompnet/analysis/broker.py
    title: Analysis broker
  - id: escalation
    resource: src/decompnet/analysis/escalation.py
    title: Escalation policy
  - id: network
    resource: src/decompnet/network.py
    title: Broker wiring
  - id: project
    resource: src/decompnet/project.py
    title: CONFIG_SCHEMA analysis/rea blocks
  - id: readme
    resource: README.md
    title: Drafts feature row
---

# Ghidra integration and the analysis broker

## Ghidra (`forks/ghidra.py`)

Ghidra is **optional**. It is large (540 MB), needs Java 21 or newer, and is pinned as the `ghidra` tool in [toolchains](/project/toolchains.md)[^toml][^ghidra].

- **Purpose.** It produces structured function dossiers for platforms m2c doesn't read: x86, AArch64, Xbox 360 PowerPC, SuperH and others[^ghidra]. In the README, m2c writes drafts for MIPS, PowerPC and ARM, and Ghidra covers "x86, AArch64 and everything else"[^readme].
- **How it runs.** `analyzeHeadless` analyses a unit's target object once. The two Java scripts in `forks/ghidra_scripts/` (`DecompWtfExport.java`, `DecompOne.java`) export every function[^ghidra].
- **Caching.** The cache key covers the target bytes, loader, language, Ghidra version and analysis options. Transient failures are never cached as empty successes[^ghidra].
- **Discovery** tries these in order[^ghidra]:
  1. `DECOMP_JAVA_HOME` / `DECOMP_GHIDRA_HOME`
  2. workspace provisioning
  3. PATH or the registry
- **Admission.** A `TargetProfile` pins the target kind, processor, endianness and memory map. Profiles that contradict the bytes are rejected with `AdmissionError`; nothing is guessed[^ghidra].

**`analyze_target(function)`** is a chat tool for ROM projects. It calls `ghidra.decompile_rom_function` and returns the decompiled C, the calls and the data references, all *read-only*. Every idea must still pass `compile_and_diff`. Without Ghidra it raises `GhidraUnavailable`[^chat].

## Analysis broker (`analysis/`)

The `Broker` provides one narrow contract over evidence providers. It handles capability admission, cache reuse, request coalescing, budgets and provenance. A provider failure is returned, never silently switched to another engine[^broker].

- **Lazy start.** The coordinator builds the broker on the first escalation, never on the claim/attempt hot paths[^network].
- **Providers.** `DirectGhidraProvider` is registered when Ghidra is available. The REA adapter is registered only with `DECOMPNET_REA_PROVIDER=1`, which is off by default[^network][^project].
- **Escalation.** `EscalationPolicy` reads the structured mismatch summary and asks for analysis only for structural or control-flow uncertainty[^escalation].
- **Defaults.** In `project.toml`, the `analysis.enabled` default is `false`, with route `direct_ghidra`[^project].

See [configuration and state](/project/configuration-and-state.md) and the [tool concepts](/tools/index.md).

[^ghidra]: src/decompnet/forks/ghidra.py
[^chat]: src/decompnet/app/chat.py
[^toml]: src/decompnet/toolchains.toml
[^broker]: src/decompnet/analysis/broker.py
[^escalation]: src/decompnet/analysis/escalation.py
[^network]: src/decompnet/network.py
[^project]: src/decompnet/project.py
[^readme]: README.md
