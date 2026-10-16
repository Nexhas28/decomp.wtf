# Workstream C — real analysis lanes: commands, outputs, outcomes

Host: macOS (darwin arm64), 2026-10-16. Every command below was executed verbatim; outputs are
quoted/summarised. Artifacts live under `/Users/shanemcgovern/Decomp/java` and the throwaway
`/tmp/wc3-lanes` area — nothing outside the workspace was installed or modified. The pinned Ghidra
install (`~/Documents/Codex/2026-10-14/i-want/work/ghidra-displaylink/ghidra_12.1.4_PUBLIC`) and the
rea-agents package (`~/.local/lib/node_modules/rea-agents`) were used read-only.

## 0. Java provisioning — final artifact: full JDK (not JRE)

- **API URL:** `https://api.adoptium.net/v3/binary/latest/21/ga/mac/aarch64/jdk/hotspot/normal/eclipse`
- **Archive:** `OpenJDK21U-jdk_aarch64_mac_hotspot_21.0.12.1_1.tar.gz`, Temurin JDK 21.0.12.1+1
- **SHA-256:** `3623232f33a9c3baadf304480b2535f9a3cba8a58d42ecbb438ba267315d9998` — matches the
  Adoptium assets API `binary.package.checksum` for image_type=jdk exactly.
- Extracted to `jdk-21.0.12.1+1/`; `bin/java` → `openjdk version "21.0.12.1" (Temurin-21.0.12.1+1)`.
- **Why a JDK and not a JRE:** a JRE-only Temurin 21 (sha256
  `dec50fc6f9fcd4fe3ae8cabf5a5fa68f6afc48841f7698e468e9aa5d54beed84`, also API-verified) was
  provisioned first and satisfied Ghidra's own launcher, but REA's Ghidra doctor rejected it:
  `{"code": "unsupported_version", "detail": "21.0.12.1; 64-bit; runtime only; ...; accepted a 64-bit
  full JDK 21 or newer", "name": "java", "ok": false}` — so it was replaced by the full JDK.
- Also present on this host (read-only, not wired): Homebrew OpenJDK 21.0.12.1 at
  `/opt/homebrew/opt/openjdk@21` — Ghidra's LaunchSupport prefers it when it can resolve it.

## 1. Discovery wiring (src/decompnet/forks/ghidra.py + decompnet/analysis/rea.py)

`java_home()` = DECOMP_JAVA_HOME → `~/Decomp/java/*/Contents/Home` → PATH (normalised to the dir with
`bin/java`). `ghidra_home()` = DECOMP_GHIDRA_HOME → toolchain registry → pinned workspace install.
`available()` = ghidra_home() && java_home() && java_version() >= 21. `_run_headless` sets JAVA_HOME
for the subprocess and honours `DECOMP_GHIDRA_USER_HOME` by adding `-Duser.home=<dir>` to BOTH
GHIDRA_JAVA_OPTIONS and JAVA_TOOL_OPTIONS (the latter reaches the LaunchSupport step, which persists
`java_home.save` to `~/Library/ghidra` before GHIDRA_JAVA_OPTIONS is applied — a real sandbox
failure fixed here). `rea.allowed_env()` now injects JAVA_HOME and GHIDRA_INSTALL_DIR into the REA
sidecar allowlist (REA's `GhidraInstallation.js` reads exactly those two variables).

Smoke check: `java_home → /Users/shanemcgovern/Decomp/java/jdk-21.0.12.1+1/Contents/Home`,
`java_version → 21`, `ghidra_home → .../ghidra_12.1.4_PUBLIC`, `available → True`.

## 2. Real-lane bugs found and fixed (evidence the lanes are real)

1. `-loader Elf` → `InvalidInputException: Invalid loader name specified: Elf`. analyzeHeadless
   resolves `-loader` via `LoaderService.getLoaderClassByName` (verified in HeadlessOptions
   bytecode): the accepted values are loader CLASS names. Fixed: `_LOADER_NAMES` =
   ElfLoader/PeLoader/MachoLoader/BinaryLoader/CoffLoader.
2. `-cspec default` → `InvalidInputException: Compiler spec "default" is not supported for language
   "x86:LE:64:default"`. Compiler-spec ids are per-language. Fixed: unpinned compiler spec is now
   `None` and `-cspec` is only passed when the caller pins one (exporter summary still records the
   real one: the x86 run reports `"cspec": "windows"`).
3. Java exporter compile errors against 12.1.4 (GhidraScript compiles scripts at runtime):
   `ThunkFunction.getThunkedFunction(monitor)` does not exist → fixed to
   `getDestinationFunctionEntryPoint()`; `CodeBlock.getDestinations(monitor)` returns a
   `CodeBlockReferenceIterator` (not Iterable) → fixed to an explicit iterator loop.
4. Sandbox: Ghidra writes logs/preferences and `java_home.save` to `~/Library/ghidra` →
   `-Duser.home=<workspace dir>` via DECOMP_GHIDRA_USER_HOME (+ JAVA_TOOL_OPTIONS, see §1).
5. FID script: `FidSearchResult.matches` is a public field that stays **null** on a miss (12.1.4) →
   null-guarded; the load count is emitted into the JSONL as `{"loaded_fidbf": 10}`.

## 3. Lane (a): G-09 — real import/export through forks/ghidra.py — PASSED

Fixture: `clang -target x86_64-unknown-linux-gnu -c fixture.c -o fixture.o` →
`ELF 64-bit LSB relocatable, x86-64`, functions `add3`/`mul2`. Run through the real
`ghidra.analyze_object` pipeline (profile x86/64/little, loader ElfLoader, DECOMP_GHIDRA_USER_HOME
set). Result:

```
status: complete | wall: 4.4 s | exported: 2 | total: 2 | from_cache: False
names: ['add3', 'mul2']
  add3: entry=0x100000 ranges=[(1048576, 1048599)] cov_pseudo=ok
   C: int add3(undefined8 param_1,int param_2) { ... return unaff_EDI + unaff_ESI + param_2; }
  mul2: entry=0x100020 ranges=[(1048608, 1048621)] cov_pseudo=ok
   C: int mul2(void) { ... return unaff_EDI << 1; }
second call from_cache: True | status: complete
```

Summary file: `{"type":"summary","status":"complete","functions_total":2,"functions_exported":2,
"failed":[],"language":"x86:LE:64:default","cspec":"windows","provider":"ghidra-headless",
"version":"12.1.4",...}`. The pytest G-09 test additionally checks the dossiers against the fixture
oracle (the object's own `nm` symbol table: entry == 0x100000 + symbol offset) and cache round-trip.

## 4. Lane (b): Function ID — real search executed — PASSED (zero hits, correct)

The pinned distribution ships FID databases at `Ghidra/Features/FunctionID/data/`
(10 × `.fidbf`: vs2012/2015/2017/2019 × x64/x86 + vsOlder × x64/x86, plus building/common-symbols
tables). `FidLookupScript.java` (API verified against the FunctionID.jar bytecode: FidService/
FidProgramSeeker/FidFileManager/FidSearchResult/FidMatch) loads all 10 shipped databases and runs
`seeker.searchFunction` per function. Result:

```
{"loaded_fidbf":10}
{"function":"add3","matches":[]}
{"function":"mul2","matches":[]}
```

Zero matches is the honest expected outcome: the shipped DBs are MS Visual Studio common libraries
and the trivial clang functions do not resemble them; the rows + `loaded_fidbf:10` prove the search
really executed. A hit would carry library/version/name fields (asserted in the test).

## 5. Lane (c): BSim local pilot — signature generation + local DB executed — PASSED;
similarity-query sub-lane SKIPPED with the exact reason

- `analyzeHeadless` in 12.1.4 has NO `-bsim` flag (full usage captured, 0 matches). `support/bsim`
  CLI commands: createdatabase/setmetadata/generatesigs/commitsigs/listfuncs/dumpsigs/... — **no
  similarity-query command**.
- Executed for real:
  - `bsim generatesigs "ghidra:/tmp/wc3-lanes/bsimproj/bsimproj" /tmp/wc3-lanes/bsim-xml --config medium_64`
    → `sigs_808f65b58b9eb33ace6fcabd324c0d16` containing
    `<fdesc name="add3" addr="0x100000">` and `mul2` (md5 matches the import log).
  - `bsim createdatabase "file:/tmp/wc3-lanes/bsimdb/bsim" medium_64` → `bsim.mv.db` (local H2).
  - `bsim commitsigs "file:/tmp/wc3-lanes/bsimdb/bsim" <xml>` → "Writing signatures for sigs_808f..."
  - `bsim listfuncs "file:/tmp/wc3-lanes/bsimdb/bsim" --md5 808f65b58b9eb33ace6fcabd324c0d16` →
    ```
    808f65b58b9eb33ace6fcabd324c0d16 fixture.o x86:LE:64:default gcc
      add3 808f65b58b9eb33ace6fcabd324c0d16 fixture.o x86:LE:64:default gcc
      mul2 808f65b58b9eb33ace6fcabd324c0d16 fixture.o x86:LE:64:default gcc
    ```
- Similarity-query sub-lane skipped, exact reason (also asserted in the test): *"the local BSim
  similarity-query lane is skipped: the 12.1.4 bsim CLI has no similarity query command
  (createdatabase/generatesigs/commitsigs/listfuncs/dumpsigs only), the shipped query scripts
  (QueryFunction, LocalBSimQueryScript) are interactive, and no PostgreSQL is installed on this host
  for a local BSim server; signatures were generated and committed to a local H2 database and listed
  via listfuncs (executed for real above)"* — the plan's permitted fallback
  ("implement the analyzeHeadless signature-file generation only and mark the query lane skipped
  with the exact reason"), exceeded (generation + local DB + real database queries).

## 6. Lane (d): REA sidecar — open_binary + analyze_function — PASSED

With the full JDK discovered (JRE was rejected, see §0), the real rea-agents 6.4.0 sidecar ran
`open_binary` (Ghidra deep provider, mach-o arm64 host executable) and `analyze_function` +
`read_function_instructions`, read-only:

```
open_session ok in 0.8 s (initialize + tools/list schema pin + open_binary)
fetch status: ok | wall: 4.5 s
  x::_twice: status=ok entry=4294968488
    pseudocode: int _twice(int param_1) { return param_1 << 1; }
    instructions: 6 [{'asm': '0x1000004a8: sub sp, sp, #0x10'}, ...]
  x::_main: status=ok entry=4294968416
    pseudocode: undefined4 entry(void) { _twice(0x15); _printf("%d\n"); ... }
    instructions: 18 [...]
```

REA's Ghidra doctor passed every check against the pinned install (version 12.1.4, native
decompiler mac_arm_64, JAVA_HOME full JDK). Symbols on Mach-O carry the leading underscore
(`_twice`, `_main`) — the lane uses those names.

## 7. Real-lane tests (pytest, all executed on this host)

```
PYTHONPATH=src .venv/bin/python -m pytest tests/test_ghidra.py::test_real_lane_imports_a_clang_object_with_identity_correct_dossiers -q   → PASSED (~4-8 s)
PYTHONPATH=src .venv/bin/python -m pytest tests/test_retrieval.py::test_real_fid_lane_searches_the_shipped_databases -q                → PASSED (~4 s)
PYTHONPATH=src .venv/bin/python -m pytest tests/test_retrieval.py::test_real_bsim_lane_generates_signatures_and_populates_a_local_database -q → PASSED (~12 s)
PYTHONPATH=src .venv/bin/python -m pytest tests/test_rea.py::test_real_lane_open_binary_and_analyze_function -q                        → PASSED (~6 s)
```

Full suite (final, clean, single run): `PYTHONPATH=src .venv/bin/python -m pytest tests/ -q` →
**19 failed, 436 passed, 115 skipped, 30 errors (42.4 s)** — all 19 failures are the pre-existing
host/lane failures (fork_judge fixture, test_updates PermissionError/network, test_network,
test_platform ninja, test_app_server, test_phase23); zero failures in test_ghidra/test_retrieval/
test_rea/test_analysis. Clean baseline on the same HEAD with these five files reverted:
**29 failed, 421 passed, 117 skipped, 30 errors** — the 10 extra baseline failures are
`tests/test_rea.py` AttributeErrors, because the committed rea.py (PR-25/26 rework 05f210e) already
calls `ghidra.java_home()`/`ghidra.ghidra_home()`, which this workstream adds to forks/ghidra.py.
So the workstream delta is: −10 failures (rea tests repaired), +15 passes (5 new real-lane tests +
10 repaired rea tests), −2 skips (real lanes now RUN instead of skipping).
