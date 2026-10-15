# Benchmarks and fixtures (Phase 0)

Baseline and measurement harness for the decomp.wtf implementation plan.
This directory exists to make every claimed improvement reproducible.

## Layout

```text
benchmarks/
  baseline-manifest.json        P0.1 machine-readable inventory of the checkout/host/toolchains
  current-checkout-findings.md  P0.1 short findings log (plan assumptions vs. this checkout)
  baseline-pytest.txt           P0.4 recorded test-suite baseline
  fixtures/                     P0.2 legally distributable fixture corpus (build scripts + truth labels)
  harness/                      P0.3/P0.4 replay harness (fixed-input and live-agent runners)
  results/<run-id>/             per-run artifacts (manifest, config, attempts.jsonl, stage-timings.jsonl, ...)
```

`results/` is disposable compute output; `fixtures/` and `harness/` are committed.

## Fixture policy (P0.2)

- Only source-owned or appropriately licensed programs whose target binaries can be
  regenerated from committed build scripts.
- Truth labels (which source matches which target) live in the harness, never in agent briefs.
- A holdout set is reserved from tuning; `fixtures/` documents which subset it is.
- Linux `gcc` lanes and proprietary-toolchain lanes are recorded as skipped on hosts that
  lack them (this host: see `baseline-manifest.json`).

## How to run

```bash
# full suite (baseline):
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q

# new queue/lease tests:
PYTHONPATH=src .venv/bin/python -m pytest tests/test_permute.py -q

# replay harness (fixed-input, no model randomness):
PYTHONPATH=src .venv/bin/python benchmarks/harness/replay.py --help
```
