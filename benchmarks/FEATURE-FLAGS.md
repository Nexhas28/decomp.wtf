# Feature flags and disablement map (plan P12.4)

Every optimisation below can be disabled independently; version-safe acceptance, verified queue
correctness and the judge are baseline behaviour, not optional safety features.

| Plan flag | Implemented as | Enable/disable |
|---|---|---|
| `compile_cache` | cache.CompileCache in compiler.py (P2.4-P2.6) | `cfg.cache=False`, `DECOMPNET_COMPILE_CACHE=0`, or `DECOMPNET_CACHE_DIR` |
| `compile_single_flight` | in-process producer/waiter inside the same cache | disabled with the cache |
| `rom_batch_compile_once` | RomNetwork.attempt_many (P2.2) | always on for ROM batches; fork path unchanged |
| `immutable_include_snapshots` | cache snapshot staging (P2.7) | disabled with the cache (legacy copy path) |
| `prepared_target_cache` | scoring.prepare() LRU (P3.1) | bounded LRU, `prepare_cache_clear()`; policy registry versioned |
| `score_only_permuter` | scoring.score_only / Comparison (P3.2) | the permuter uses score_only; render paths unchanged |
| `m2c_prepared_context` | seeds.prepare_context (P4.2) | context-free retry always labelled; cache via `DECOMPNET_M2C_CACHE` |
| `guided_permuter` | search/ package + Permuter wiring (P5.4-P5.6) | search policy registry versioned; `beam=1` restores single-trajectory parity |
| `structural_retrieval` | similarity FeatureIndex cache (P6.2-P6.3), always on | in-memory; `invalidate_all()`; index version exposed; the structural term is controlled by `DECOMPNET_STRUCTURAL_RERANK` |
| `ghidra_rich_evidence` | forks/ghidra.py dossier (P7.3) | old name->C projection kept; legacy cache files simply not reused |
| `analysis_escalation` | analysis/ broker + escalation policy (P8) | policy thresholds configurable; rules auto-disable after cost-without-progress |
| `rea_provider` | analysis/rea.py provider adapter (P9) | off by default; capability matrix admission; read-only |
| `function_id_retrieval` / `bsim_retrieval` | retrieval/ package (P10) | off by default; no external queries unless explicitly configured |
| `adaptive_scheduler` | "priority" claim strategy + scheduler.py (P11.2) | opt-in strategy; other strategies untouched |
| `DECOMPNET_GUIDED_SEARCH` | search/policy.py `active_policy()` (`guided_v1` vs `single_v1`) | off by default (single-trajectory search); `DECOMPNET_GUIDED_SEARCH=1` enables guided beam search |
| `DECOMPNET_STRUCTURAL_RERANK` | similarity.py `structural_rerank_enabled()` (structural term of the combined score) | off by default; `DECOMPNET_STRUCTURAL_RERANK=1` enables it |
| `DECOMPNET_REA_PROVIDER` | network.py `analysis_broker` (registers the REA provider on the analysis broker) | off by default; `DECOMPNET_REA_PROVIDER=1` enables it when REA is available |

Rollback for any slice: revert the slice's commit; accepted-source history is never cleared as a
cache-repair action (runbook 24.2).
