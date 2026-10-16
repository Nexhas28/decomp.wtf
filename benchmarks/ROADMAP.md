# Wave-3 roadmap

Item list for Wave 3 with owners and status. Evidence backing the three approval-gated items
lives in `benchmarks/APPROVALS.md` (re-executed 2026-10-16 at HEAD `7f113c7`). No item here is
enabled by default; approving a record authorises the approach, not a flag flip.

**Status 2026-10-16 (Wave-3 close-out):** every non-gated item is landed and tested. The four
remaining boxes are deliberate: three need a human signature (`APPROVALS.md`), one is a live-agent
run that needs an endpoint + cost decision, and one is deferred assembler work.

## Landed this wave

| Item | What it is | Owner | Status |
| --- | --- | --- | --- |
| A.1 | Model routing (plan P11.5) | calibration | **Landed** — configurable model identities per task class with measurement hooks; optional (no routing config = unchanged behaviour). `tests/test_model_routing.py` 12 passed. |
| A.3 | Broker/escalation into the live loop (plan P8.4) | ghidra | **Landed** — stall notes carry the escalation policy's next action; `tests/test_escalation_wiring.py` 12 passed. |
| A.5 | Calibration admission + held-out validation (plan P4.1) | calibration | **Landed** — `calibrate_and_commit` refuses to commit without held-out validation; `tests/test_calibration_commit.py` 17 passed. |
| A.7 / A.10 | Multi-seed ablations + grouped-vs-single drafting + holdout recall (plan P12.2/P4.5) | compile-cache → lead | **Landed** — `benchmarks/harness/stats.py` (Wilson intervals, no-overclaim `compare`), `draft_compare.py` (equal-budget grouped vs single, `unavailable` when m2c is absent), `holdout_recall.py` (recall@k + verified-reuse; refuses a corpus containing an answer). `tests/test_ablate_harness.py` 7 passed. |
| A.8 / A.9 | Section 20 straggler tests (O-02, O-04..O-08, W-02/W-03/W-06) | scoring-core | **Landed** — `pause_acceptance`/`resume_acceptance` helper + ops/scheduler tests; 19 passed. |
| A.14 | Retrieval acceptance feedback (plan P6.5) | similarity | **Landed** — `retrieval/feedback.py` route provenance + holdout leakage gate wired into `on_match`/`find_similar`; 12 passed. |
| C.2 | maspsx load-delay-nop drop (spike) | permutation | **Landed, opt-in, off by default** — `rom/maspsx.py` + versioned asm-pass registry; 17 tests incl. 2 real byte-level lanes. Spike finding: the safe pattern is an ASPSX-original property; the demo corpus contains only the unsafe case (correctly kept), and the inverse wall (needing an *insert* pass) is documented. |
| C.4 | Boundary merge execution (plan P8.6) | lead | **Landed** — `merge_function` validated + a **real bug fixed**: `propose_merge` and `merge_function` disagreed on adjacency direction, so no reviewed proposal could ever execute. `tests/test_merge.py` 7 passed. |
| C.5 | `claim closest` size cap | lead | **Landed** — 400-insn cap on the near-miss strategy (giants no longer handed out); by-name and `hardest` unaffected. |
| C.6 | Bridge identity split-brain | lead | **Landed** — stable per-bridge client id (`agents.bridge`) makes a restarted daemon reuse its original identity instead of a suffixed one; concurrent clients still get separate rows. |

## Still open (deliberate)

| Item | What it is | Owner | Status |
| --- | --- | --- | --- |
| A.11 | Upstream `decomp-permuter` integration approach (plan P5.7) | drafts (approval record) | Adapter landed (PR-19, `search/external.py`, 7 tests green at HEAD); discovery-gated and **not installed on this host** (`external.available()` = False); not wired into the default search path. Approval record in `APPROVALS.md` pending human sign-off. |
| A.12 | REA provider enablement (plan P9) | drafts (approval record) | Real lanes **pass**: live schema smoke vs rea-agents 6.4.0 (initialize + tools/list, 170 tools, pin matches) and Mach-O `open_binary` + `analyze_function` decompile; `tests/test_rea.py` 42/42. GAP-1..GAP-8 (verbatim from `rea.py` docstring) recorded with consequences + checkboxes in `APPROVALS.md`, pending sign-off. Production enablement additionally needs a pinned Ghidra + **full JDK 21+** on the deployment host (workspace JDK exists at `~/Decomp/java/jdk-21.0.12.1+1/Contents/Home`; `DECOMP_JAVA_HOME` wiring documented in `forks/ghidra.py`). |
| A.13 | BSim/FID retrieval corpus admission (plan P10) | drafts (approval record) | Function ID runs real and correct: 10 shipped `.fidbf` searched, zero hits (honest result, load count proves execution). BSim signature + local H2 database lane runs real. The similarity-**QUERY** lane cannot run: no query command in the Ghidra 12.1.4 bsim CLI (PostgreSQL / H2-server / BSim-server options listed in `APPROVALS.md`). Enabling retrieval requires admitting a reference corpus with provenance + holdout separation — admission checklist recorded, pending sign-off. |
| A.6 | Live-agent benchmark | benchmarks harness | **Harness only** — the fixed-input replay harness (`benchmarks/harness/replay.py`, plan P0.4) runs; the live-agent run requires endpoint approval + cost cap and has not been executed. |
| C.3 | `%hi`-fold permutation operator | permutation | **Deferred:** assembler feature work — folding `%hi`/`%lo` pairs needs the PSY-Q-compatible assembler path (aspsx); maspsx is pinned but an aspsx investigation is required first. No fold operator exists in `permuter.py`/`search/` today. |

## Field-driven fixes from the perf-test runs (beyond the plan)

Three production perf-test runs (documented in `work/perf-test/`) found that the C-only frontier is
saturated by toolchain walls and that stored bests were stale. Landed in response: PR-31 (legacy-best
flags, wall-aware claiming, structured wall registry, merge proposals) and C.1's outcome — the blanket
`context.h` sdata-initializer edit was **rejected by the regression gate** (it breaks two matched
functions), so the documented per-function-file recipe is the supported route.


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

## Game Boy / Game Boy Color support (Wave 4, in progress)

Requested feature. Status: **the reading lane is done and validated on a real cartridge; the matching
lane needs a C toolchain for the CPU.**

### Landed

| Piece | What it does |
| --- | --- |
| `rom/sm83.py` | Complete Sharp LR35902 disassembler written for this fork: all 256 base opcodes and all 256 CB-prefixed ones, the 11 undocumented opcodes rendered as `db $xx` (so a sweep never desynchronises), `jr`/`call`/`jp` target helpers. Capstone has no SM83 backend, which is why this exists. |
| `rom/gb.py` | Detection (Nintendo logo + header checksum + power-of-two size), full cartridge header (title with the CGB 15-byte rule, CGB flag/mode, MBC, ROM/RAM size, checksums, SGB flag, size-mismatch reporting), 16 KiB banking, function discovery (RST/interrupt vectors + entry + `call`/`jp` targets, sequential sweep **and** byte scan) and disassembled function records. |
| `rom/detect.py` | `gb`/`gbc` registered as platforms; `.gb`/`.gbc` files are detected, and the "not a ROM" message now lists them. |
| `engine._import_gb` | Importing a Game Boy ROM creates a real project: platform, 16 KiB segments, every discovered function with its disassembly, symbols and a context version. Functions are stored as `asm` (visible, searchable, briefable) and deliberately **not claimable** — no agent should burn compiles on a lane that cannot judge a candidate yet. |
| `app.js` | `GB`/`GBC` labels in the sidebar so imported projects read correctly. |

Evidence (real cartridge, `Legend of Zelda, The - Link's Awakening DX (Rev 2) (SGB Enhanced) (GB Compatible).gbc`,
1 MiB): detected `gbc` (flag 0x80 → cgb-enhanced), `MBC5+RAM+battery`, 64 banks, SGB flag set, header
checksum verified; **3,108 functions discovered in 9.8 s** with 758 resolved call references, entry
vector `nop` / `cp $11` at 0x150 (the real CGB compatibility check), largest function 228 instructions.
Tests: `tests/test_gb.py` — 85 passing, including an end-to-end import test.

### Next (matching lane)

1. **Toolchain**: an SDCC/GBDK (or RGBDS + a C compiler) profile in `toolchains.py`, with the flags a
   cartridge build actually used. Until then `diff` on a Game Boy function answers with the honest
   "no compiler profile" message.
2. **Bank-aware relocation model**: the CPU sees 0x4000-0x7FFF switched by the MBC, so two banks can
   share a CPU address. Functions are currently addressed by flat ROM offset; the matcher needs
   bank+address pairs, plus MBC bank-register writes as relocations.
3. **Symbol/split conventions**: pret/pokered-style naming (`LoadGBPal`, `wram_*`) and a splat-like
   split driven by the discovered call graph rather than fixed 0x400 extents.
4. **Draft generation**: m2c is MIPS-only, so Game Boy needs either its own SM83→C draft or none.
