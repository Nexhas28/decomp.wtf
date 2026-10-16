# Ablation and evidence record (plan P12.2 / Section 21)

Honest accounting: this host (macOS arm64, Apple M5 Pro) cannot run the Linux-toolchain lanes
(projects/demo x86-64 gcc fixtures, fork objdiff/dtk builds, test_updates network lanes). What is
measured here is deterministic and reproducible; what is NOT measured is listed per slice and stays
an explicitly-untested lane per plan policy.

## Baseline
- Suite at reviewed commit 575b4a3 on this host: 107 passed / 17 failed / 115 skipped / 30 errors
  (failures = macOS environment lanes: Linux gcc flags rejected by clang, missing ninja/objdiff,
  network-dependent update tests).
- Recorded: benchmarks/baseline-pytest.txt, benchmarks/baseline-manifest.json.

## Slice-by-slice evidence (as-committed)

| Slice | Commit | Deterministic evidence | Untested lanes |
|---|---|---|---|
| PR-03 queue repair | 40caa02 | Q-01 fixture (80 exhausted / 20 eligible) fails before, passes after; restart persistence | real-compiler permuter runs |
| PR-04 leases/budgets | 6d76760 | Q-02 atomic lease (two threads), Q-03 failed claim charges nothing, Q-04 expiry recovery, Q-05 restart, Q-06 reset policy | multi-process store contention |
| PR-05 snapshots/receipts | 7cb6408 | digest sensitivity per dependency class; receipt/epoch on match; revision bumps; restart | cross-host digest equality |
| PR-06/07/08 acceptance | 017f4c2 | V-01..V-08 + X-01a-d (13 tests, 10 red-first, forced interleavings, no sleeps) | live room network |
| PR-09/10 compile cache | cb9e17a | C-01..C-12 stubbed runner; real-gcc byte parity + cross-process smoke (worker-verified) | bwrap/docker/WSL runtimes |
| PR-11 ROM batches | b8e7aab | C-01 one compile for six functions (compile-count assertion), C-02 partition | PSX real toolchain batch timing |
| PR-12/13 scoring | 58ea504 | S-01..S-07 golden parity vs vendored pre-change copy; encoding/reloc counterexamples | native-alignment throughput comparison (S-07 gate exists) |
| PR-14 calibration | 9b451ad | D-07 inconclusive-never-replaces; identity sensitivity per field | held-out calibration on a real project |
| V-05 gate | 76deed8 | compiler_digest delegation cross-checked (64 tests) | cross-host remote snapshots |
| PR-15 m2c context | 37c2378 | D-01..D-05, D-08 + real-m2c lane (toolchains.installed gate) | IDO cpp lanes (host substitute, labelled degraded) |
| PR-16 provenance | 5ecfe88 | 6 tests: origins, dedup, cap, snapshot freshness | long-horizon storage growth |
| PR-16b grouped drafts | f951a21 | D-06 variants: neighbourhood, bounds, per-unit context | grouped-vs-single budget ablation (Linux lane) |
| PR-17 operators | 7f3052a | P-01..P-03 + determinism (27 tests) | real search solve-rate |
| PR-18 guided search | 325b11b | P-04..P-06, routing table, checkpoint/resume, budget epoch (25 tests) | beam ablation under fixed budget (Linux lane) |
| PR-19 external adapter | e8b54ac | P-07 reverification, single-flight, timeout, JobSpec rejection (7 tests) | real decomp-permuter (not installed) |
| PR-20/21 similarity | 637cb53 | R-01..R-06: cached features, invalidation, tail distinction, twin retry | holdout recall/precision corpus |
| PR-22/23 ghidra | e33ca7e | G-01..G-10 mocked + exporter jar verification vs pinned 12.1.4 (worker) | real analyzeHeadless (no JDK on host) |
| PR-24 broker | db822f7 | A-01..A-07 fake provider, event-driven | real provider end-to-end |
| PR-29 scheduler | 4eef5cd | W-01 explainability/ordering, W-01b priority claim, W-04 budget, W-05 truthful scope | live swarm A/B scheduling |
| PR-02 telemetry | cad9b77 | disabled-no-op, spans/counters/thread-local, attempt-path stages | cross-process correlation |
| PR-30 ops | 804b983 | O-01 WAL-safe backup restore (regression-found), O-01b no fabricated receipts, O-03 publication invariant | full-migration restore on production DB copy |

## What has NOT been claimed
- No end-to-end speed-up numbers (no Linux lane here): only deterministic compile-count and
  correctness assertions. The fixed-input harness (benchmarks/harness/replay.py) is the intended
  measurement vehicle where toolchains exist.
- No solve-rate claims for the guided search, Ghidra evidence, REA, FunctionID or BSim: those
  remain capability-gated/disabled until their admission gates run on admitted targets.
- Integrated suite after Wave 3: 359 passed / 19 failed / 116 skipped / 30 errors — failure and
  error sets byte-identical to the environment baseline.

## Rollback
Every slice is independently revertible (one commit each); accepted-source history is never part of
cache repair. See benchmarks/FEATURE-FLAGS.md and benchmarks/RUNBOOKS.md.
