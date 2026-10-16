# Follow-on completion record (rollout + wiring + real lanes + ablations)

Executed 16 Oct 2026 after the PR-01..PR-30 fork was complete. Everything below is committed
or retained as artifacts; the goal's four workstreams all landed.

## 1. Phase 12 rollout — DONE (production runs the fork)
- Canary-first: fork wheel staged in `/Users/<user>/Decomp/canary-venv`, run with
  `DECOMP_HOME=canary-home` on port 19388 against a WAL-safe copy of the live DB. Evidence:
  the copy gained the fork's tables on open, and an end-to-end MCP smoke (real gcc-2.8.1-psx
  compile of func_800A1D6C, score 78210) recorded an attempt WITH an attempt_snapshots row.
- Production cutover executed twice (initial + final rebuild including Workstreams B/C):
  WAL-safe backups at `.decomp/backups/pre-cutover-20261016-001806.db` and
  `pre-cutover2-20261016-004146.db`; the fork wheel installed into the live uv tool venv
  via `uv pip install --reinstall --no-deps`; app restarted on 6464. A bridge-race left two
  brief instances (6464/6465) — both killed; the swarm bridges self-healed to exactly one
  fork instance. Verified: single listener (pid on 6464), /health ok, the live DB has the
  fork's tables (attempt_snapshots/candidates/search_budgets/verification_receipts), and the
  swarm (14 workers, 1210/1661 matched at last check) continues matching through the fork's
  acceptance path. Rollback: stock wheel + backup restore per benchmarks/RUNBOOKS.md 24.0.
  Script: benchmarks/rollout/rollout.sh.

## 2. Retrieval engine wiring — DONE (commit 40ce237)
- `reference_sources` table (Section 18.6 + Registry round-trip fields, per-project scopes,
  shared corpus via project_id NULL), `save/load_reference_sources` bridging, `config_from(proj)`
  parsing the optional `retrieval` config block (unknown keys rejected, flags off by default),
  `find_similar` appends routed FID/BSim candidates with REQUIRES VERIFICATION labels (byte-
  identical when disabled), `RomNetwork.retrieval_candidates` + `retrieval_leakage_check` hooks.
  12 tests, red-first; suite delta +12 passes.

## 3. Real analysis lanes — DONE (commit fe542e3)
- Temurin JDK 21.0.12.1+1 provisioned in `/Users/<user>/Decomp/java` (Adoptium checksum
  verified; full JDK required by REA's Ghidra doctor). Discovery wiring: `DECOMP_JAVA_HOME` /
  workspace / PATH; `DECOMP_GHIDRA_HOME`; sandbox-safe `-Duser.home` relocation.
- Real executions now PASS: G-09 real import/decompile vs nm oracle; Function ID loads and
  searches all 10 shipped .fidbf databases; BSim XML signature generation + local H2 database +
  listfuncs queries (similarity-query sub-lane skipped: no query command in 12.1.4); REA
  open_binary + analyze_function decompiles a real Mach-O through the Ghidra bridge. The lanes
  caught and fixed 5 real bugs (loader class names, -cspec default, ThunkFunction API break,
  CodeBlockReferenceIterator, ~/Library/ghidra sandbox write). Evidence:
  benchmarks/real-lanes/REAL-LANE-RESULTS.md.
- REA adapter reworked schema-accurately against the live 6.4.0 catalog (commit 05f210e,
  41 tests incl. real-lane smoke); broker GAP-8 fixed (route metadata excluded from
  correlation identity). GAP-1..GAP-8 recorded for human approval before enabling the real route.

## 4. PSX solve-rate ablation — DONE (commit 11b0169)
- benchmarks/harness/ablate.py: guided-v1 beam-4 vs baseline beam-1 at equal 300-step budgets,
  cold/warm cache pairing, real gcc-2.8.1-psx compiles, in-memory scoring (zero DB writes), on a
  WAL-safe copy of the production DB. Records: benchmarks/ablation-record/PSX-REPORT.md +
  /Users/<user>/Decomp/benchmarks/results/psx-ablation/ (8,404 step rows).
- Result: solved 0/7 vs 0/7 on the 8 easiest open near-misses — an honest null result (single
  seed, arm64/qemu timing, easiest-sample bias all documented). Guided evaluates ~2.8x
  candidates at ~2.8x compiles; warm cache cuts real compiles ~20%. One genuine toolchain wall
  (binutils-2.6 "Illegal operands lui/addiu") and one search wall classified, not crashed.
  NOT claimed as equivalence — noted as null at this sample size.

## Final state
- Integrated suite: 436 passed / 19 failed / 115 skipped / 30 errors; the only failures beyond
  the original environment baseline are the two GitHub-reachability update tests.
- Production: fork running (single instance), swarm healthy, pre-cutover backups retained.
- Known gaps a human must approve before enabling optional real routes: REA GAP-1..GAP-8
  (module docstring) and the BSim similarity-query sub-lane (no CLI command in Ghidra 12.1.4).
