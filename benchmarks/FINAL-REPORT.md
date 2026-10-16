# Final delivery report (plan Section 27) — PR-01..PR-30 coverage

Working tree: /Users/<user>/Decomp/decomp-wtf, base 575b4a30 (the plan's reviewed commit).
Every row below maps a plan PR to its commit and evidence. Status: ✅ = implemented + tested +
committed; ⏳ = in flight.

## PR coverage

| Plan PR | Commit | Status | Test files |
|---|---|---|---|
| PR-01 baseline + fixtures | 290e747 | ✅ | benchmarks/baseline-manifest.json, baseline-pytest.txt |
| PR-02 telemetry | cad9b77 (+06f3776 harness) | ✅ | tests/test_telemetry.py |
| PR-03 permuter queue repair | 40caa02 | ✅ | tests/test_permute.py (Q-01..) |
| PR-04 leases + budgets | 6d76760 | ✅ | tests/test_permute.py (Q-02..Q-06) |
| PR-05 snapshots/receipts | 7cb6408 | ✅ | tests/test_snapshots.py |
| PR-06 version-safe acceptance | 017f4c2 | ✅ | tests/test_acceptance.py (V-01..V-08) |
| PR-07 ingress unification | 017f4c2 | ✅ | tests/test_acceptance.py (X-01a..d) |
| PR-08 compile/evaluate/commit split | 017f4c2 (+b8e7aab _judge) | ✅ | parity: acceptance/claims suites |
| PR-09 include snapshots + keys | cb9e17a | ✅ | tests/test_compile_cache.py |
| PR-10 cache + single-flight | cb9e17a | ✅ | tests/test_compile_cache.py (C-01..C-12) |
| PR-11 ROM batch compile-once | b8e7aab | ✅ | tests/test_batch.py |
| PR-12 prepared targets | 58ea504 | ✅ | tests/test_scoring.py (S-01, S-05, S-06) |
| PR-13 score-only + mismatches | 58ea504 | ✅ | tests/test_scoring.py (S-02, S-03, S-04, S-07) |
| PR-14 compiler profiles + calibration | 9b451ad (+76deed8 V-05 gate) | ✅ | tests/test_calibration.py |
| PR-15 m2c prepared context | 37c2378 | ✅ | tests/test_drafts.py (D-01..D-05, D-08) |
| PR-16 provenance + grouped drafts | 5ecfe88 + f951a21 | ✅ | tests/test_candidates.py, tests/test_draft_group.py |
| PR-17 labelled operators | 7f3052a | ✅ | tests/test_permuter_ops.py (P-01..P-03) |
| PR-18 guided search | 325b11b | ✅ | tests/test_search.py (P-04..P-08) |
| PR-19 upstream permuter adapter | e8b54ac | ✅ | tests/test_external_permuter.py (P-07) |
| PR-20 twin invalidation + features | 637cb53 | ✅ | tests/test_similarity.py |
| PR-21 structural reranking | 637cb53 | ✅ | tests/test_similarity.py (R-01..R-06) |
| PR-22 rich Ghidra exporter | e33ca7e | ✅ | tests/test_ghidra.py (G-01..G-10 mocked) |
| PR-23 Ghidra lifecycle | e33ca7e | ✅ | tests/test_ghidra.py |
| PR-24 analysis broker | db822f7 | ✅ | tests/test_analysis.py (A-01..A-07) |
| PR-25 REA adapter | ⏳ | ⏳ | tests/test_rea.py (E-01..E-09) |
| PR-26 REA persistence/recovery | ⏳ | ⏳ | tests/test_rea.py |
| PR-27 reference registry + FID | ⏳ | ⏳ | tests/test_retrieval.py (B-01..B-06) |
| PR-28 local BSim pilot | ⏳ | ⏳ | tests/test_retrieval.py |
| PR-29 scheduler + budget + UI | 4eef5cd | ✅ | tests/test_scheduler.py (W-01, W-04, W-05) |
| PR-30 validation + rollout | 804b983 + 70d3a2d + this report | ✅ (core) | tests/test_operations.py (O-01, O-03) |

## Section 20 catalogue coverage

Implemented-and-executed: Q-01..Q-06, V-01..V-08, X-01, C-01..C-12, S-01..S-07, D-01..D-05,
D-06, D-08, P-01..P-08, R-01..R-06, G-01..G-10 (mocked; real lane skips on this host), A-01..A-07,
E-01..E-09 (in flight), B-01..B-06 (in flight), W-01, W-04, W-05, O-01, O-03.
Not implemented as standalone tests: D-07 full held-out lane (P4.1 held-out validation is engine-side
follow-up), W-02/W-03/W-06 (covered by Q-06, existing brief bounds and the benchmark manifest),
O-02, O-04..O-08 (partially covered by existing test_network/policy fixtures; documented in
benchmarks/ABLATIONS.md as untested lanes).

## Exit-gate checklist (Section 27)

- [x] Queue starvation and lease/budget accounting fixed and tested (Q-01..Q-06)
- [x] Current-snapshot acceptance protects all local and remote result paths (V-01..V-08, X-01)
- [x] Context edits validate the correct evolving matched set (matched epochs, V-02/V-03)
- [x] Compatible ROM batches compile once (C-01 compile-count assertion)
- [x] Cache identity covers material inputs and preserves source policy (C-01..C-12)
- [x] Include staging immutable and sandboxed (P2.7 snapshots, C-11)
- [x] Prepared targets and score-only evaluation preserve verdicts (S-01..S-07 golden parity)
- [x] Compiler profiles and m2c context explicit and reproducible (D-01..D-05, D-07 variant)
- [ ] Mutation/search changes improve measured verified outcomes — measured only where toolchains
      exist; deterministic replay + parity proven, solve-rate ablation remains an untested lane
      (documented in benchmarks/ABLATIONS.md; the plan itself requires the change to be opt-in
      otherwise — the guided search is versioned and beam=1 restores single-trajectory parity)
- [x] Twin reuse and fork build semantics intact (R-04/R-05; fork tests byte-identical to baseline)
- [x] Rich Ghidra records have correct identity, coverage, provenance (G-01..G-10)
- [x] Analysis admitted per host/format/mode, budgeted, provider-failure-safe (A-01..A-07)
- [ ] REA pinned, schema-tested, read-only, optional — in flight (real schema discovery succeeded:
      rea v6.4.0 tools/list captured)
- [ ] FunctionID/BSim provenance + judge path — in flight
- [x] Migrations/backups/cancellation/cleanup verified (O-01 WAL-safe backup regression)
- [x] Feature disablement exercised (FEATURE-FLAGS.md; every flag mapped to its control point)
- [x] Integrated suite evidence retained (benchmarks/integrated-suite.txt)

## Definition of completion

Core correctness/efficiency work is implemented, tested and committed; optional analysis
integrations are capability-gated adapters that default off and keep the ordinary matching path
independent of them (the plan's own completion definition). Remaining: the two in-flight adapters
(PR-25/26, PR-27/28) review+commit and the final integrated run — this file is updated at that
point.
