# Current-checkout findings (Phase 0 / P0.1)

Working tree: `575b4a30ed481ff5497dfb9ce4f599368a95a04a` (`Discontinue decomp.wtf`, package 0.10.6) — identical to the reviewed revision, so every [D##] finding from the plan was re-verified against the exact code. Differences between the plan's assumptions and this checkout are listed here; the machine-readable version is `baseline-manifest.json`.

## Re-verified plan findings (all still present at HEAD)

| Finding | Evidence |
|---|---|
| D01 — permuter SQL applies `LIMIT` before the Python turn-budget filter | `app/permute.py:68-73`: `SELECT ... ORDER BY best_score/n_insns LIMIT ?` with `limit * 4`, then `if self._turns.get(...) < MAX_TURNS` — exhausted high-ranked functions can hide eligible ones and return an empty list |
| D01b — turn budget is in-memory only | `app/permute.py:47` `self._turns: dict[int, int]`; restarts grant unlimited repeated work (plan P1.1 step 4 / Q-05) |
| D01c — turns charged before the claim is acquired | `app/permute.py:95` increments `_turns` before `_permute`, whose first act `_hold` can fail (line 118-119) — failed claims consume turns (plan P1.2 / Q-03) |
| D03/V-01 — attempt reads the context outside the record transaction and never revalidates it | `network.py:392-417`: `ctx = self._context(...)`, compile happens outside any tx, `_record` (461-480) marks `status='matched'` without checking the context version is still latest |
| D03/V-02 — `edit_context` validates only the header version, not the matched set | `network.py:504-526`: regression test runs before the tx; commit checks only `_latest_version != ctx["version"]`; a function matched during the regression test can be silently broken by the edit |
| D02/D03 — ROM batch compiles the same source once per function | `app/engine.py:1023` `attempt_many` (read in PR-11 slice) |
| D06 — m2c context strips preprocessor directives and retries without context | `seeds.py` (read in PR-15 slice) |
| D09 — similarity prefix-limited at 800 instructions | `similarity.py` (read in PR-21 slice) |
| D12 — Ghidra exporter cache assumes any JSON file present is a complete success | `forks/ghidra.py` (read in PR-22 slice) |

## Deviations from plan assumptions

- The plan's `src/decompnet/...` paths match this checkout exactly (repo root = package source under `src/decompnet`).
- `local_ext.py` in `~/.local/share/decomp-wtf/source` (an older 0.10.2 snapshot of this repo plus local patches) is **not part of this repository** and is not the implementation target. This repo at 0.10.6 supersedes it.
- The production app on this machine runs the published 0.10.6 wheel; the checked-out tree is byte-equivalent to that version but is not yet deployed. Rollout to the running app is a Phase 12 activity (canary-first) and is out of scope for core PRs.

## Test-environment caveats (baseline)

`107 passed, 17 failed, 115 skipped, 30 errors` on macOS arm64. Failures/errors are environment lanes, not regressions:

- `projects/demo` fixtures need a Linux `gcc`; on macOS `gcc` is clang and rejects `-fno-reorder-blocks-and-partition`/`-flive-patching=inline-only-static`. These lanes stay explicitly untested here (plan: leave unavailable toolchain lanes explicitly untested).
- Fork-judge/forks tests need objdiff/dtk builds (Linux).
- `tests/test_updates.py` needs network/uv paths the sandboxed dev env does not provide.

The relevant real-tool lane on this host is PSX (gcc 2.7.2/2.8.1 PSX toolchains and m2c are installed) plus agbcc for GBA.
