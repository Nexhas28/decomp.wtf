# Wave-3 roadmap

Item list for Wave 3 with owners and status. Evidence backing the three approval-gated items
lives in `benchmarks/APPROVALS.md` (re-executed 2026-10-16 at HEAD `7f113c7`). No item here is
enabled by default; approving a record authorises the approach, not a flag flip.

| Item | What it is | Owner | Status |
| --- | --- | --- | --- |
| A.11 | Upstream `decomp-permuter` integration approach (plan P5.7) | drafts (approval record) | Adapter landed (PR-19, `search/external.py`, 7 tests green at HEAD); discovery-gated and **not installed on this host** (`external.available()` = False); not wired into the default search path. Approval record in `APPROVALS.md` pending human sign-off. |
| A.12 | REA provider enablement (plan P9) | drafts (approval record) | Real lanes **pass**: live schema smoke vs rea-agents 6.4.0 (initialize + tools/list, 170 tools, pin matches) and Mach-O `open_binary` + `analyze_function` decompile; `tests/test_rea.py` 42/42. GAP-1..GAP-8 (verbatim from `rea.py` docstring) recorded with consequences + checkboxes in `APPROVALS.md`, pending sign-off. Production enablement additionally needs a pinned Ghidra + **full JDK 21+** on the deployment host (workspace JDK exists at `~/Decomp/java/jdk-21.0.12.1+1/Contents/Home`; `DECOMP_JAVA_HOME` wiring documented in `forks/ghidra.py`). |
| A.13 | BSim/FID retrieval corpus admission (plan P10) | drafts (approval record) | Function ID runs real and correct: 10 shipped `.fidbf` searched, zero hits (honest result, load count proves execution). BSim signature + local H2 database lane runs real. The similarity-**QUERY** lane cannot run: no query command in the Ghidra 12.1.4 bsim CLI (PostgreSQL / H2-server / BSim-server options listed in `APPROVALS.md`). Enabling retrieval requires admitting a reference corpus with provenance + holdout separation — admission checklist recorded, pending sign-off. |
| A.6 | Live-agent benchmark | benchmarks harness | **Harness only** — the fixed-input replay harness (`benchmarks/harness/replay.py`, plan P0.4) runs; the live-agent run requires endpoint approval + cost cap and has not been executed. |
| C.3 | `%hi`-fold permutation operator | permutation | **Deferred:** assembler feature work — folding `%hi`/`%lo` pairs needs the PSY-Q-compatible assembler path (aspsx); maspsx is pinned but an aspsx investigation is required first. No fold operator exists in `permuter.py`/`search/` today. |

## Gate behaviour today

- All three approval-gated routes (A.11/A.12/A.13) remain disabled by default and change nothing
  about the default match path until a human signs the corresponding record in `APPROVALS.md`.
- A.6 is measurable today only through the fixed-input harness; a live run is blocked on
  endpoint approval + cost cap, not on harness code.
- C.3 is an open feature work item, not a gate.

## Re-verification commands

```bash
cd /Users/shanemcgovern/Decomp/decomp-wtf
PYTHONPATH=src .venv/bin/python -m pytest tests/test_external_permuter.py -q   # A.11 evidence (7 tests)
PYTHONPATH=src .venv/bin/python -m pytest tests/test_rea.py -q                 # A.12 evidence (42 tests, incl. real lanes)
PYTHONPATH=src .venv/bin/python -m pytest tests/test_retrieval.py -q           # A.13 evidence (incl. real FID/BSim lanes)
PYTHONPATH=src .venv/bin/python -m pytest tests/test_analysis.py -q            # broker surface
```
