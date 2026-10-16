# Approvals required before optional routes enable (Wave 3)

Three decision records a human must sign off before the corresponding route can be enabled.
Everything here is evidence-based: commands and tests were re-executed on this host (macOS
darwin arm64) on 2026-10-16 at HEAD `7f113c7` (PR-31). None of the three routes is enabled by
default today; signing a record only authorises the approach, it does not flip a flag by itself.

- A.11 — upstream `decomp-permuter` integration approach (plan P5.7)
- A.12 — REA provider enablement (plan P9)
- A.13 — BSim/FID retrieval corpus admission (plan P10)

---

## A.11 — Upstream decomp-permuter integration approach (plan P5.7)

### What exists

The adapter `src/decompnet/search/external.py` (PR-19, commit `e8b54ac`) drives the upstream
tool (https://github.com/simonlindholm/decomp-permuter) as an *optional* route. It is
discovery-gated, tested, and **not wired into the default search path** — the internal guided
search (PR-18) is the only enabled mutation route today. 7 tests in
`tests/test_external_permuter.py` cover it; all 7 pass at HEAD:

```
PYTHONPATH=src .venv/bin/python -m pytest tests/test_external_permuter.py -q   → 7 passed
```

### What the adapter does

- Discovery is explicit: a configured executable path, or `decomp-permuter`/`permuter` on PATH.
  Absent ⇒ `available()` is False and the internal route runs, as always.
- One bounded upstream run per invocation: the tool is executed with no arguments inside a
  prepared work directory; the caller's wrapper script (written into the workdir) is responsible
  for producing the CLI contract.
- Scores are **advisory** (the upstream scale differs): every returned candidate is recompiled
  and re-verified through the coordinator (`reverify()` → `net._judge`) before it can improve
  anything — the judge is the only acceptance path (plan P5.7 steps 4-5).
- Jobs for distributed use carry a versioned `JobSpec` (policy + seed + snapshot key); a worker
  that cannot resolve the policy version must reject the job (`SearchPolicyError`), never resubmit
  guesses (P5.8 / P-08).

### CLI contract the adapter expects

The tool must write `scores.json` into the work directory:

```json
{"scores": {"name": 1234}, "candidates": {"name": "/* proposed source */"}}
```

`scores` must be an object (otherwise the run is an `error`); `candidates` is optional.

### Guarantees

| Guarantee | Behaviour |
| --- | --- |
| Single-flight | One external run at a time process-wide (module lock); a concurrent request returns `status="busy"` instead of oversubscribing the host (P5.7 step 6) |
| Timeout | Hard timeout (default 300 s); on expiry the run is `status="timeout"` and nothing is published |
| No partial publication | Non-zero exit, missing/ill-formed `scores.json`, or a non-object `scores` are all `status="error"`; only a parsed contract is `ok` |
| Reverify | `reverify()` recompiles and re-judges every candidate through the coordinator; upstream scores are never trusted for acceptance; one odd candidate cannot break the batch |
| Provenance | No upstream code is vendored — the adapter is original code; if an upstream operator is ever copied in, its license notice must ship alongside (P5.7 step 7) |

### Host state (verified 2026-10-16)

The tool is **NOT installed on this host**: `toolchains.installed("decomp-permuter")` is `None`,
`discover()` misses both names, and `search.external.available()` is `False`. The real-lane test
in the plan therefore cannot run here and is recorded as skipped-with-reason until an operator
installs a pinned revision.

### Decision required (plan P5.7 step 7: "obtain project approval for the integration approach")

Approving this record authorises: the adapter-only integration approach (no vendored upstream
code), advisory upstream scores, mandatory coordinator re-verification, single-flight execution,
and the requirement that the pinned upstream revision be recorded in deployment manifests at
adoption time.

- [ ] **Approved** — adopt the upstream decomp-permuter integration approach as specified above
      (adapter-only, advisory scores, coordinator re-verification, single-flight, pinned revision
      recorded in deployment manifests before any enablement).
- [ ] Rejected / needs changes (note below).

Approver: ____________________　Date: ____________　Notes: ______________________________

---

## A.12 — REA provider enablement (plan P9)

### What exists and what passes for real

The adapter `src/decompnet/analysis/rea.py` (PR-25/26, commit `05f210e`) drives the pinned
`rea-agents` **6.4.0** MCP server as a private stdio sidecar behind the analysis broker, and is
read-only by default. The real lanes were re-executed at HEAD and pass:

- `tests/test_rea.py` → **42 passed**, including:
  - `test_real_lane_schema_discovery` — live `initialize` + `tools/list` against the installed
    6.4.0 package matches the pinned catalog (170 tools; capture at `/tmp/rea_tools_list.json`,
    reproducible with `benchmarks/harness/rea_discover.py`).
  - `test_real_lane_open_binary_and_analyze_function` — a real Mach-O arm64 host executable is
    decompiled read-only through the Ghidra deep provider: `open_binary` (ephemeral database) +
    `analyze_function` return pseudocode and instructions for `_twice`/`_main` (full output in
    `benchmarks/real-lanes/REAL-LANE-RESULTS.md` §6).
- Supporting real lanes also pass at HEAD: Ghidra 12.1.4 import/decompile vs the `nm` oracle,
  Function ID search, and BSim signature/database generation (see A.13).

### Gap list a human must approve before enabling the real route

Reproduced verbatim from the `src/decompnet/analysis/rea.py` module docstring (HEAD `7f113c7`),
each with its consequence and a checkbox:

- GAP-1: the documented raw-binary route (CPU family + entry_point mapping) is absent from pinned
  6.4.0; every matrix-passing raw profile is typed-unsupported (``rea_raw_route_absent``).
  → **Consequence:** headerless raw binaries cannot be analysed through REA; the direct Ghidra
  adapter remains the only route for them.
  - [ ] accepted
- GAP-2: no type-inventory tool -> the ``types`` facet is always ``unavailable``.
  → **Consequence:** type evidence is never advertised; consumers get an explicit `unavailable`.
  - [ ] accepted
- GAP-3: ``get_call_graph`` nodes carry addresses only -> neighbourhoods use named dossier
  callees; graph traversal is not wired to a facet.
  → **Consequence:** neighbourhood expansion is limited to named dossier callees, no graph walks.
  - [ ] accepted
- GAP-4: relocatable objects (ET_REL) are not admitted (the pinned target model has no verified
  object lane).
  → **Consequence:** `.o`/ET_REL inputs are refused with a typed reason; headered executables only.
  - [ ] accepted
- GAP-5: big-endian headered ELF outside the pinned MIPS lane is not admitted (no verified lane).
  → **Consequence:** big-endian non-MIPS ELF is refused; the pinned MIPS32r2 o32 lane is the only
  big-endian lane.
  - [ ] accepted
- GAP-6: the adapter pins the package version exactly (``6.4.0``); upgrading REA requires
  re-pinning the schemas here (E-01 fails first, which is the intended behaviour).
  → **Consequence:** any REA upgrade is a code change with schema re-pinning, never a config swap.
  - [ ] accepted
- GAP-7: Windows P0 runtime facts (NTFS path, native addon, Windows 10+) are detected from the
  pinned package layout where possible and must otherwise be supplied explicitly — never assumed.
  → **Consequence:** a Windows deployment must supply runtime facts explicitly; nothing is assumed.
  - [ ] accepted
- GAP-8: the broker's correlation payload includes the provider-specific profile dictionary; the
  engine identity is identical across both routes, but exact group *equality* additionally needs
  broker-side normalisation (profile dicts carry route-specific bindings) — a broker follow-up.
  → **Consequence:** cross-route evidence may be correlated by engine identity, but exact group
  equality is not claimed until broker-side profile normalisation lands.
  - [ ] accepted

### Production enablement additionally requires (deployment host)

- A pinned Ghidra install (wired via `DECOMP_GHIDRA_HOME`), and
- a pinned **full JDK 21+** — REA's Ghidra doctor rejects JRE-only runtimes
  (`unsupported_version: "runtime only"`, verified against Temurin 21 JRE vs JDK).
  The workspace JDK on this host exists: `~/Decomp/java/jdk-21.0.12.1+1/Contents/Home`
  (Temurin 21.0.12.1+1, Adoptium checksum-verified). Discovery order — `DECOMP_JAVA_HOME` →
  workspace provisioning → PATH — and the sandbox-safe `-Duser.home` relocation via
  `DECOMP_GHIDRA_USER_HOME` are documented in `src/decompnet/forks/ghidra.py`
  (`java_home()`, `ghidra_home()`, `_run_headless`).

### Decision required

- [ ] **Approved** — enable the real REA route with GAP-1..GAP-8 accepted as recorded above, on a
      deployment host that provisions the pinned Ghidra install and full JDK.
- [ ] Rejected / needs changes (note below).

Approver: ____________________　Date: ____________　Notes: ______________________________

---

## A.13 — BSim/FID retrieval corpus admission (plan P10)

### What runs for real (re-executed at HEAD)

- **Function ID — real, correct, zero hits.** The pinned Ghidra 12.1.4 distribution's 10 shipped
  `.fidbf` databases (vs2012/2015/2017/2019 × x64/x86 + vsOlder × x64/x86) are loaded
  (`loaded_fidbf: 10`) and searched per function via the FID API. On the clang fixture the result
  is `matches: []` for every function — the honest expected outcome (shipped DBs are MS Visual
  Studio common libraries; the fixture functions do not resemble them). The load count proves the
  search really executed; a hit would carry library/version/name and remains a *candidate*,
  never a verdict (`matched` is invariant False).
  `tests/test_retrieval.py::test_real_fid_lane_searches_the_shipped_databases` → **PASSED**.
- **BSim signature + database lane — real.** `bsim generatesigs` (medium_64) produced signatures
  for the fixture functions, `createdatabase`/`commitsigs` populated a local H2 database, and
  `listfuncs` answered real database queries.
  `tests/test_retrieval.py::test_real_bsim_lane_generates_signatures_and_populates_a_local_database`
  → **PASSED**.

### What cannot run today: the BSim similarity-QUERY lane

Verified against the real 12.1.4 CLI (usage captured on this host):

- `analyzeHeadless` has **no `-bsim` flag** (full usage, 0 matches).
- `support/bsim` commands are: `createdatabase`, `setmetadata`, `getmetadata`, `addexecategory`,
  `addfunctiontag`, `dropindex`, `rebuildindex`, `prewarm`, `generatesigs`, `commitsigs`,
  `generateupdates`, `commitupdates`, `listexes`, `getexecount`, `delete`, `listfuncs`,
  `dumpsigs` — **no similarity-query command**.
- The shipped query scripts (`QueryFunction`, `LocalBSimQueryScript`) are interactive (GUI), and
  no PostgreSQL is installed on this host for a local BSim server.

Options that would enable a real query lane (each needs its own decision later):

1. **PostgreSQL-backed BSim server** — `bsim createdatabase postgresql://…` plus a BSim server
   (or the interactive scripts / GUI client against it); requires a PostgreSQL install on the
   deployment host.
2. **H2 database + interactive client** — query the local `file:/…` H2 database through the
   shipped GUI scripts; no headless query exists in 12.1.4.
3. **BSim server / ElasticSearch backends** — `https://` / `elastic://` URL forms exist in the
   CLI; both require standing up an additional service.

Until one of these lands, the similarity-query sub-lane stays skipped with this exact recorded
reason (asserted in the test): *the 12.1.4 bsim CLI has no similarity query command
(createdatabase/generatesigs/commitsigs/listfuncs/dumpsigs only), the shipped query scripts
(QueryFunction, LocalBSimQueryScript) are interactive, and no PostgreSQL is installed on this
host for a local BSim server; signatures were generated and committed to a local H2 database and
listed via listfuncs (executed for real above)*.

### Corpus admission checklist (from `src/decompnet/retrieval/registry.py`)

Enabling retrieval requires admitting a reference corpus with provenance and holdout separation;
`admit_retrieval()` refuses a requested route that has no usable, permitted entry. Each admitted
entry must carry:

- [ ] **Identity:** repository + pinned version, toolchain/profile the artifact was built under,
      and the target-identity digest of the built artifact.
- [ ] **Provenance:** how the entry was obtained and admitted (reviewed, not guessed).
- [ ] **Usage restrictions:** explicit tuples (`source-denied`, `no-redistribution`,
      `internal-only`, …); entries without accessible, permitted source are investigation
      evidence only, never reusable C.
- [ ] **Holdout separation:** benchmark answers are marked `holdout` and are never indexed;
      `leakage_check` is run and clean (no answer matches an entry by source digest, target
      identity, or normalised source location).
- [ ] **One usable entry per enabled route:** Function ID and BSim each require at least one
      usable, permitted registry entry, or the feature stays disabled.
- [ ] **BSim index readiness:** usable signatures exist in the index, and any cold-build cost
      stays under the configured `bsim_max_cold_cost` cap.
- [ ] **No silent external traffic:** `external_queries` stays off unless explicitly enabled;
      no remote queries or uploads by default.
- [ ] **Candidates, never verdicts:** everything retrieved is compile-and-verified through the
      coordinator before any acceptance; FID/BSim output is never marked `matched`.

### Decision required

- [ ] **Approved** — admit the reference corpus specified in the deployment manifest (provenance
      recorded per entry, holdout separation verified, leakage check clean) and enable the
      retrieval routes as listed above. Note: enabling *BSim similarity queries* additionally
      requires choosing one of the three query-lane options above — that choice is a separate
      decision.
- [ ] Rejected / needs changes (note below).

Approver: ____________________　Date: ____________　Notes: ______________________________
