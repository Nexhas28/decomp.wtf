# Operational runbooks (plan Section 24, adapted to the implemented fork)

## 24.0 Backups and migrations (plan P12.3)
Back up with SQLite's backup primitive (`sqlite3.connect(src).backup(dst)` / `VACUUM INTO`), never a
raw file copy of the `.db` while WAL mode is active — a raw copy silently drops the WAL
(regression-covered in tests/test_operations.py::test_o01). Apply additive schema changes
transactionally; old caches are disposable and live in a separate namespace; legacy matched rows
without receipts stay matched but are never promoted to receipted verification.

## 24.1 An accepted result fails clean revalidation
1. Pause new acceptance for the affected project/scope; read-only inspection stays up.
2. Retain the source, object, target, receipt (`verification_receipts`), snapshot
   (`attempt_snapshots`) and relevant logs.
3. Compare dependency identities with the current project: context version/digest, target digest,
   compiler profile identity (`toolchains.profile_identity`), matched epoch.
4. Classify: stale context / cache corruption / changed toolchain / changed target-layout / policy drift.
5. Disable the affected optimisation (see benchmarks/FEATURE-FLAGS.md), never the verification gate.
6. Re-evaluate affected accepted sources under a coherent current snapshot
   (`Network.regression_test`).
7. Restore only validated current state; keep historical source (`candidates` table).
8. Add a deterministic regression fixture before re-enabling.

## 24.2 Compilation/evaluation cache suspected corrupt
1. Disable cache reads (`cfg.cache=False` / `DECOMPNET_COMPILE_CACHE=0`).
2. Quarantine suspect entries (the cache quarantines tampered payloads automatically;
   `CompileCache.diagnose()` lists them).
3. Recompile from immutable original inputs.
4. Compare payload and key identities (entry manifests carry the full toolchain manifest).
5. Fix the key/publishing defect, bump `CACHE_SCHEMA`.
6. Re-enable on fixtures, then a canary project. Never clear accepted-source history as a repair action.

## 24.3 Permuter reports no work unexpectedly
1. Compare eligible open functions before and after budget/claim filters (Q-01 fixture shape).
2. Inspect stale claims, exhausted budgets (`search_budgets.turns_used/max_turns`), cooldowns and
   epochs (`search_budgets.epoch` = search-policy epoch).
3. Confirm eligibility filters precede LIMIT (PR-03 invariant) and that policy-version changes grant
   the intended fresh budget (PR-18).
4. Check zero/unknown instruction counts are excluded.
5. Resume only genuinely eligible work; never erase all budgets as a routine fix.

## 24.4 Ghidra or REA becomes unavailable
1. Mark the analysis route unavailable with the original failure reason (broker returns typed failures;
   no silent failover).
2. Ordinary matching/drafting continues.
3. Drain or fail queued analysis requests explicitly; completed evidence keeps its identity.
4. Close owned sessions and verify cleanup (SessionManager.dispose_all / broker close_idle_sessions).
5. Check the pinned runtime/artifact, host admission and target mapping.
6. Retry under a bounded policy or choose another admitted route. Never reuse evidence from a different
   profile/engine as equivalent.

## 24.5 Shared type/symbol information changes
1. Submit the edit through the regression-protected path (edit_context/rename_symbol — epoch-checked).
2. The commit is the new dependency epoch (context version + matched epoch).
3. Invalidate affected draft/evaluation/evidence/index entries (digests key everything).
4. `flushCache()` on metadata changes; reset/reimport when the Program view is invalidated.
5. Controlled retries for functions whose search assumptions changed (budget reset reason
   'context-changed').

## 24.6 A benchmark improves time but reduces matches
1. Verify equal total budgets and equivalent verification.
2. Separate cache microbenchmarks from end-to-end solve rate (benchmarks/harness/replay.py is the
   fixed-input lane).
3. Inspect scoring-policy drift, lost candidate diversity, prematurely stopped functions.
4. Review cold-start/failed-analysis costs and holdout composition.
5. Retain the faster implementation only with an explicit acceptable trade-off; otherwise disable.
