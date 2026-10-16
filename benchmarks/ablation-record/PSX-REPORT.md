# Workstream D — PSX real-toolchain solve-rate ablation (plan Section 21)

Run: **psx-ablation/run-002** (300-step budget; `run-001` is the 120-step pilot, both retained).
Repo commit under test: **05f210e** (full PR-01..PR-30 fork, as recorded by the harness; the tree
moved to fe542e3 — Workstream C — after these runs, no impact on the recorded paths).
Harness: `benchmarks/harness/ablate.py`. Started 2026-10-16T00:34:57Z, finished 2026-10-16T00:38:48Z.

## What was measured

Guided permuter search (`guided-v1`, beam 4) vs a baseline single-trajectory policy (same
routing/operators, beam 1), under **equal step budgets** (300 steps per function per variant),
on a **copy** of the production slus-01363-4 database, with the compile cache **paired cold/warm**.
Real compiles run the project's pinned `gcc-2.8.1-psx` profile; candidates are evaluated by the
coordinator's own ROM-lane scoring (`RomNetwork._evaluate` link + `scoring.score_only`), in memory,
never written back to the copy. "Matched" = the search reached score 0 (byte-identical candidate)
under that scoring; nothing is accepted on the copy.

Sample: the 8 easiest open near-misses by the permuter's own priority order (score/n_insns ratio),
excluding asm-marker sources. Deterministic seeds: `ablate:<policy>-v<ver>:<function-id>`, identical
for the cold and warm arms of the same policy (identical trajectories, so the cold/warm comparison
is a pure cache effect).

## Environment and honest deviations

- Host: macOS arm64; the harness runs inside `python:3.12-bookworm` on OrbStack
  (Linux aarch64 VM, qemu binfmt for the i386 toolchains). Timings are **not** representative of
  production x86-64 hardware; counts and outcomes are.
- **prlimit disabled in the harness process** (documented deviation): the coordinator's per-compile
  resource caps (`prlimit --as=2147483648`) make qemu-emulated i386 toolchains fail with
  "Unable to find a guest_base" (the 2 GiB RLIMIT_AS prevents qemu-i386 from reserving the guest
  address space). The harness compiles only project-recorded sources on a disposable DB copy;
  production compiles keep their caps.
- **No database writes**: candidate evaluations are scored in memory only, so each policy starts
  from the same recorded state (a recorded improvement would otherwise change the other arm's
  starting point). Verified: 0 new `attempts` rows, function state unchanged after both runs.
- No src/ changes were needed for the harness (it consumes `RomNetwork`, `search.SearchRun` and
  `compiler.compile_source` as-is; the compile cache is PR-09/10, cacheable negatives included).

## DB copy (RUNBOOKS 24.0)

SQLite backup API, never a raw WAL copy:

    python3 - <<'EOF'
    import sqlite3
    src = sqlite3.connect("file:/Users/<user>/Decomp/.decomp/decomp.db?mode=ro", uri=True)
    dst = sqlite3.connect("/Users/<user>/Decomp/benchmarks/results/psx-ablation/app.db")
    with dst: src.backup(dst)
    EOF

Copy state at backup time: **1209 matched / 361 open / 1661 total, integrity ok** (the brief's
"1169/402" predates the copy; the live DB progressed in the meantime — the copy is a consistent
point-in-time snapshot). Verified again after both runs: state unchanged, integrity ok.

## Exact run commands

See `psx-ablation/run.sh` (run-001) and `psx-ablation/run-002.sh` (run-002). The run-002 command:

    docker run --rm --platform linux/arm64 \
      -v /Users/<user>/Decomp/decomp-wtf:/w:ro \
      -v /Users/<user>/Decomp/slus-01363-4:/Users/<user>/Decomp/slus-01363-4:ro \
      -v /Users/<user>/Decomp/benchmarks:/Users/<user>/Decomp/benchmarks \
      -v "$HOME/.local/share/decomp-wtf/backup/decomp-wtf-home-volume/.local/share/decompnet/toolchains":/Users/<user>/.local/share/decomp-wtf/backup/decomp-wtf-home-volume/.local/share/decompnet/toolchains:ro \
      -e DECOMPNET_TOOLCHAINS=/Users/<user>/.local/share/decomp-wtf/backup/decomp-wtf-home-volume/.local/share/decompnet/toolchains \
      -e PYTHONPATH=/w/src \
      python:3.12-bookworm bash -c 'pip install -q capstone pyelftools rabbitizer pyyaml && \
        python3 /w/benchmarks/harness/ablate.py \
          --db /Users/<user>/Decomp/benchmarks/results/psx-ablation/app.db \
          --project-root /Users/<user>/Decomp/slus-01363-4 \
          --results-root /Users/<user>/Decomp/benchmarks/results \
          --run-id psx-ablation/run-002 --sample 8 --max-steps 300 --timebox 1140 --per-run-wall 240'

## Per-function results (run-002, 300 steps)

| function | unit | insns | start | guided final | baseline final | guided real compiles (cold/warm) | baseline real compiles (cold/warm) | note |
|---|---|---|---|---|---|---|---|---|
| func_8003ED00 | main | 154 | 10 | 10 (budget-exhausted) | 10 (budget-exhausted) | 168/168 | 54/11 | guided: 95 accepted, 59 compile errors |
| func_8003D564 | main | 161 | 15 | — | — | — | — | **toolchain wall** (see below) |
| func_800B878C | ovl_omake | 145 | 25 | 25 (budget-exhausted) | 25 (budget-exhausted) | 8/4 | 4/4 | 80/144 propose failures |
| func_8004EADC | main | 46 | 10 | 10 (budget-exhausted) | 10 (budget-exhausted) | 19/8 | 11/11 | 138/148 propose failures |
| func_8003F1C0 | main | 67 | 15 | 15 (budget-exhausted) | 15 (budget-exhausted) | 180/134 | 58/58 | guided: 94 accepted, 137 duplicate-object |
| func_80050E8C | main | 79 | 30 | 30 (budget-exhausted) | 30 (budget-exhausted) | 124/90 | 59/59 | |
| func_800B3FD8 | ovl_omake | 77 | 30 | 30 (budget-exhausted) | 30 (budget-exhausted) | 58/44 | 14/14 | |
| func_80046C80 | main | 137 | 60 | 60 (budget-exhausted) | 60 (budget-exhausted) | 2/0 | 2/2 | **search wall**: 256-258/300 propose failures (unsupported constructs) |

All finished runs are **censored (budget-exhausted)** — none matched, none improved on the recorded
best in 300 steps. The 120-step pilot (run-001) has the same qualitative shape: 0 solved, 0 improved.

## Solved / improved per policy

| metric | guided-v1 (beam 4) | baseline (beam 1) |
|---|---|---|
| solved (score 0) | 0 / 7 ran functions | 0 / 7 ran functions |
| improved over recorded best | 0 | 0 |
| wall classifications | 1 (shared) | 1 (shared) |

## Cold vs warm compile counts (run-002, all functions)

| arm | compile calls | real compiler runs (cache misses) | wall |
|---|---|---|---|
| guided cold (cache off) | 560 | 559 | 88.9 s |
| guided warm (cache on) | 560 | 448 | 80.2 s |
| baseline cold | 203 | 202 | 33.8 s |
| baseline warm | 203 | 159 | 27.7 s |

Warm arms replay the identical seeded trajectories, so every warm hit is reuse of a source already
compiled by another arm (shared probes, overlapping mutations, and cached negatives included).
Guided evaluates **~2.8×** more distinct candidates than baseline at the same step budget
(beam 4 keeps several parents alive), at a correspondingly higher compile cost.

## Classifications recorded (no crashes)

- **Toolchain wall — func_8003D564**: the recorded best attempt does not compile under the
  project's gcc-2.8.1-psx profile: `Assembler messages: Error: ERROR: Illegal operands 'lui' /
  'addiu'` (maspsx-generated source, recorded under a gcc-2.7.2-psx lineage). All four variants
  skipped with this classification.
- **Search wall — func_80046C80**: the mutation engine proposes nothing useful
  (propose_failure 256-258/300; 1680 unsupported constructs in the source). Both arms stall
  identically.
- **Budget exhaustion**: the remaining 7 functions × 4 arms all ended censored at 300 steps.

## What this supports and does not support

- **Supports**: the harness, the cold/warm pairing, the toolchain-wall classification and the
  equal-budget protocol all work end-to-end on real PSX compiles against the production project
  copy (760+ real gcc-2.8.1-psx runs in run-002). The warm cache reduces real compiles on replayed
  trajectories (~20% guided, ~21% baseline here).
- **Does not support**: any solve-rate advantage for guided-v1 over the beam-1 baseline on this
  sample — both arms solved **0/7** and improved **0/7**. This is a null result at a 300-step
  budget on the eight easiest near-misses, not evidence of equivalence (see limitations). Notably,
  guided's extra exploration (2-3× more candidates and compile errors per step budget) bought no
  score gains here, i.e. its cost was paid without a payoff in this regime.

## Limitations

1. **Environment**: arm64/qemu-emulated Linux — counts are real, timings are not representative
   of production x86-64; the Linux x86-64 demo lane remains untested (as in the brief).
2. **Budget**: 300 steps ≈ 1 turn of the production permuter (400 steps/turn, up to 4 turns);
   both arms may be underpowered to close 10-60-point gaps on near-optimal sources.
3. **Single seed per (function, policy)**: no seed repetition or confidence intervals; per plan
   21.4, stochastic-policy claims would need paired multi-seed runs.
4. **Sample**: the 8 easiest open near-misses (permuter priority order) — a convenience sample,
   not a stratified/random draw; `not-a-near-miss` functions beyond the window were recorded as
   skipped in `corpus-manifest.json`.
5. **No acceptance/verification receipts** on the copy (nothing matched, so nothing was accepted;
   `verification-receipts.jsonl` / `provider-status.jsonl` from the 21.6 layout are N/A — no
   external providers and no acceptance occurred).
6. **prlimit disabled** inside the benchmark container (see deviations); compile budgets in
   production include those caps.
7. One accounting quirk: the toolchain-wall function's probe compile counts in `compile_calls`
   but not in `real_compiles` (its arm aborted before the counter was resolved).

## Artifacts

```
benchmarks/results/psx-ablation/
  app.db                    the SQLite-backup copy of the live DB (untouched by the runs)
  run.sh / run-002.sh       exact commands
  run-001/  run-002/        manifest.json, corpus-manifest.json, summary.json,
                            attempts.jsonl (every step: operator, kind, score, delta, compiles),
                            stage-timings.jsonl (per-variant wall/compile accounting),
                            report.md (this report), cache-warm/ (the warm cache namespace)
```
