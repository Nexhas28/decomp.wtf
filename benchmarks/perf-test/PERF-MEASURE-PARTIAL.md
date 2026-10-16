# PERF-MEASURE run — second production sample (decomp identity `perf-measure`)

Status: COMPLETE (stopped after 5 functions / 18 compiles / ~73 min — under all caps:
8 functions, 60 compiles, 90 min). All claims released; production shows "working now: nobody".

This was the SECOND sample; the primary run's report already landed and PR-31
(legacy-best flags, wall-aware claiming, merge proposals) was deployed off it.

## Per-function table

| function | insns | start best | compiles | results (score) | final | matched | classification |
|---|---|---|---|---|---|---|---|
| func_8003680C | 113 | 240 (stale) | 6 (3 zombie-corrupted) | 400, garbage×3, 555, 10810 | 400 | no | wall:constant-hoist-scheduling |
| func_8003D564 | 161 | 15 (stale) | 7 | asm-reject, 2735, 1175, 1590, +3 probes | 1175 | no | wall:assembler-jtbl + gp_rel-sdata + load-delay-nop + regalloc |
| func_8003B150 | 20 | 10 | 1 | 10 (exact reproduce) | 10 | no | wall:regalloc-coalescing (`lbu $a0,($a2)` tie) |
| func_8003A628 | 14 | 10 (stale) | 2 | 210, 210 | 210 | no | wall:pipeline-drift (%hi-fold + load-delay nop) |
| func_8003B2D0 | 33 | 925 | 1 | 925 (exact reproduce) | 925 | no | wall:boundary live-in $t0 |

Totals: 5 functions, 18 compile invocations (13 clean scored, 3 zombie-corrupted,
1 assembler-reject, 1 claim-error), 0 matches, 0 stalls, 1 within-run progress
(diagnostic), 4 walls.

Latencies (mcp_agent invocation wall time): compile_and_diff round-trips
0.04–0.12 s (median ≈ 0.08 s); claim/get/progress 0.05–0.1 s. Identical infra
speed to the primary run.

Biggest single-edit score drop: 2735 → 1175 (−1560) on func_8003D564, by forcing
the gp-relative globals into .sdata with `= 0` initializers in the source.

## What this sample adds beyond the primary report

1. ZOMBIE SAME-IDENTITY COLLISION (not in the primary report): a leftover
   background loop from the previous perf-test session ran concurrent MCP
   sessions under `perf-test`/`perf-test_2`. Effect: my claims were stolen
   ("you hold another claim" mid-function) and three compiles returned
   deterministic garbage — "parse error before `(null)'" plus binary
   identifier bytes — consistent with two sessions sharing per-agent temp
   state (the compiler read a half-written file). The lead killed the daemon
   and revoked the identities; I completed the run as `perf-measure`.
   Lesson: the MCP server does not protect one agent identity against two
   concurrent sessions; a second run of the same identity must ensure the
   first process is dead.

2. STALE-BEST CORROBORATION (independent second confirmation of the primary
   finding): stored bests scored under the old pipeline do NOT reproduce —
   240→400, 15→assembler-reject ("Illegal operands lui/addiu" — the stored
   best no longer even compiles), 10→210. Recent bests reproduce byte-exact
   (10, 925). PR-31's legacy-best flags address exactly this.

3. gp_rel SMALL-DATA TRAP (new detail): gp-relative codegen requires the
   global to be in .sdata, i.e. declared WITH an initializer (`s32 X = 0;`);
   a tentative definition (`s32 X;`, what context.h v52 has for D_800626E8
   and D_80062718) lands in .bss and compiles to lui/%lo full-address form.
   Also: once in .sdata, `&X` folds to `addiu $a2, $gp, %gp_rel(X)` while the
   ROM materialises `lui/addiu %hi/%lo(X)` for address-of arguments — the
   same-symbol split-spelling conflict (GUMBO_SUPER_175 class). Both need
   lead-side fixes (header initializers + per-access spelling). I could not
   use edit_context (fresh identity, production shared state) and would not
   have edited shared context from a perf run.

4. ASSEMBLER-WALL BYPASS CONFIRMED on the current pipeline: snorlax's
   computed-goto-through-extern-table recipe (`extern void *jtbl_...[];
   goto *tbl[n];` + `&&label` keep-stubs) compiles and diffs on production
   (v3/v4/v5 all built). The wall is bypassable, but the gp_rel and
   load-delay-nop walls then dominate the residual.

5. PR-31 BEHAVIORS OBSERVED LIVE: release notes containing wall signatures
   were auto-classified — "Wall recorded (load-delay-nop): strategy claims
   skip func_... for 6h or until its context/compiler changes" (seen for
   load-delay-nop and boundary-fragment). Confirms the wall-aware claiming
   path works end-to-end from a worker's release note.

6. Minor observations: "Best attempt so far: score X by ?" for legacy
   attempts (attribution lost) vs a named zombie-erased identity
   ("erased-863") for a recent one; claim auto-release on new claim (one
   claim per agent); lease 30 min renewed per compile; no-progress nudge at
   ~7 compiles.

## Honest read

Second sample fully agrees with the primary run: infrastructure is fast and
stable (~80 ms judge round-trips, wall-gating works, notes durable); the
C-only frontier is saturated and frozen by pipeline drift plus a small set of
compiler/assembler walls. The remaining unlockable work is lead-side: sdata
initializers in context.h, aspsx-faithful %hi-fold, live-in register /
boundary-merge support, and the split-spelling lever for same-address
symbol/literal pairs. No further agent time on the current pool is justified
until PR-31's flags land and the header fixes ship.
