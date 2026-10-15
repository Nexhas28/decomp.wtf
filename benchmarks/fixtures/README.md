# Fixture corpus (P0.2)

Corpus dimensions the plan requires, and where each is satisfied:

| Dimension | Fixture | Compiler needed |
|---|---|---|
| Queue/budget/lease scheduling (Q-01..Q-06) | `tests/test_permute.py` builds 100 synthetic functions in SQLite | none (pure python) |
| Claim/context races (V-01..V-08) | `tests/test_acceptance.py` with a stubbed compile/evaluate pair | none (stubbed) |
| Cache keys/invalidation (C-01..C-12) | `tests/test_compile_cache.py` | none (stubbed compiler) |
| Small/medium/large functions, signedness, structs, loops, switches | `projects/demo` (Linux gcc lane) + PSX lane fixtures | gcc (Linux) / PSX gcc 2.7.2, 2.8.1 |
| PSX toolchain real-lane | `projects/demo-psx` (added in PR-11) | gcc-2.8.1-psx |

Holdout: `projects/demo/holdout.txt` lists functions never used to tune mutation or
retrieval weights; live-agent benchmark runs exclude them from tuning context.
