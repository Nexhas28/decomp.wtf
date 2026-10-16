# Perf-test evidence (three runs, 16 Oct 2026)

Three independent perf-test runs against the fork measured serving/compile behaviour and end-to-end
decompression throughput on a real project (`slus-01363-4`). Retained verbatim as field evidence.

| File | Run | Server | Scope |
| --- | --- | --- | --- |
| `PERF-REPORT-RUN2.md` | run 2 (test instance) | :19388 | 8 functions, 17 compiles, ~57 min |
| `PERF-TEST_REPORT_6464.md` | run 1 (production) | :6464 | 8 functions, 11 compiles, ~11 min |
| `PERF-MEASURE-PARTIAL.md` | run 3 (production, `perf-measure`) | :6464 | 5 functions, 18 compiles, ~73 min |

## Convergent findings

1. **Infrastructure is not the bottleneck.** Median judge round-trip ≈0.08 s (0.04–0.12 s range) with
   36+ fleet workers active, claims ~0.05–0.93 s, no outage across any run.
2. **The C-only frontier is saturated.** 0 matches from 21 distinct function attempts (0 progress, 1
   stall, 4–7 walls per run): every claimable function carries a compiler/assembler wall (allocator
   ties, `%hi`-fold drift, `lb`/`lbu` load-dest ties, GTE/COP2, jump tables, load-delay nops, live-in
   registers, boundary fragments).
3. **Stored best scores were stale.** 3/3 and 3/5 sampled bests failed to reproduce (240→400,
   15→assembler-reject, 240→1720, 10→610, 10→210). Only recent bests reproduced byte-exact. This is
   what PR-31's `best_verified` flag + `mark_legacy_unverified`/`revalidate_bests` address.
4. **Wall knowledge was locked in prose.** Successors re-swept walls the notes already described →
   PR-31's structured `function_walls` registry + wall-aware claiming (6 h cooldown, by-name override).
5. **Field defects fixed in response:** bridge identity split-brain on daemon restart (suffixed
   identity rows → stolen claims + garbage compiles), `claim closest` drifting to 1594-insn giants,
   boundary fragments handed out with no merge path.

## Follow-up disposition

- Fixed and deployed: PR-31 (legacy-best verification, wall registry/gating, merge proposals),
  claim-closest size cap, bridge stable client id.
- Attempted and **correctly rejected** by the regression gate: a blanket `context.h` sdata-initializer
  edit (it breaks two matched functions); the documented per-function-file recipe is the supported
  route, recorded as a note on the affected function.
- Deferred as assembler/toolchain work: a maspsx load-delay-nop *insert* pass (the inverse of the
  landed drop pass), `%hi`-fold support, and executing merges on the live project.
