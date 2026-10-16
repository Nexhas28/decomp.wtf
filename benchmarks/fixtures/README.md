# Fixture corpus (P0.2)

Corpus dimensions the plan requires, and where each is satisfied:

| Dimension | Fixture | Compiler needed |
|---|---|---|
| Queue/budget/lease scheduling (Q-01..Q-06) | `tests/test_permute.py` builds 100 synthetic functions in SQLite | none (pure python) |
| Claim/context races (V-01..V-08) | `tests/test_acceptance.py` with a stubbed compile/evaluate pair | none (stubbed) |
| Cache keys/invalidation (C-01..C-12) | `tests/test_compile_cache.py` | none (stubbed compiler) |
| Small functions | `projects/demo/original/game.c`: `abs_s32`, `rng_next`, `add_score`; also `projects/demo-n64/original/game.c` (same function list, used by `tests/test_phase23.py` and `tests/test_export.py`) | gcc (Linux); gcc-2.7.2-kmc (N64) |
| Medium functions | `projects/demo/original/game.c`: `clamp_s32`, `entity_default_hp`, `count_alive_entities`, `checksum_save` | gcc (Linux); gcc-2.7.2-kmc (N64) |
| Large functions | `projects/demo/original/game.c`: `game_tick`, `entity_spawn`, `entity_update_physics` | gcc (Linux); gcc-2.7.2-kmc (N64) |
| Exact duplicates and nonduplicates | not covered yet (no fixture function pair is checked for duplicate bodies) | - |
| Signedness and narrowing | `projects/demo/original/game.c`: `s16 type` widened in `entity_default_hp`, `u32` in `rng_next` and `checksum_save`, `u8 *` in `checksum_save`; `projects/demo-rom/src/main.c` `u8` buffers | gcc-2.7.2-kmc (N64) |
| Structures and pointer arithmetic | `projects/demo/include/context.h` (`Entity`, `Player`), `projects/demo/original/game.c` (`entity_update_physics`, `entity_spawn`); `projects/demo-rom/src/main.c` (`*(s32 *)(save_buffer + 16)`) | gcc (Linux); gcc-2.7.2-kmc (N64) |
| Loops and switches | `projects/demo/original/game.c`: `count_alive_entities`, `checksum_save`, `game_tick` (loops); `entity_default_hp` (switch) | gcc (Linux); gcc-2.7.2-kmc (N64) |
| Floating-point literals | `projects/demo-gc/crt0.c` (`1.5f`, `0.25f`); `projects/demo/original/game.c` `lerp` uses `f32` but has no literal; `projects/demo-psx/psxrt.c` float helpers (no literals) | MWCC GC 1.3.2 (GC); none for the Linux lane |
| External calls and relocations | `projects/demo-rom/src/main.c` (calls to zlib `compress`, `uncompress`, `crc32`, `adler32`, `zlibVersion`, and to `game.c` functions); `projects/demo-ds/dsplat.c` (`extern` `dtcm_scratch`, `g_frame_count`) | gcc-2.7.2-kmc (N64); MWCC 2.0 (DS) |
| Translation-unit interactions | `projects/demo-ds/dsplat.c` reads globals defined in another unit; `projects/demo-rom/src/main.c` includes `game.h` and calls `game.c` functions | MWCC 2.0 (DS); gcc-2.7.2-kmc (N64) |
| Header-driven changes | `projects/demo/include/context.h` and `projects/demo/original/game.h` (types and `extern` globals); `tests/test_period_compilers.py` reads `projects/demo-n64/include/context.h` as seed input; no header-change case found in the tests listed here | gcc (Linux); gcc-2.7.2-kmc (N64) |
| Processor modes | MIPS: `projects/demo-n64`, `projects/demo-psx` (`exe.ld` `OUTPUT_ARCH(mips)`); ARM and Thumb: `projects/demo-gba/crt0.s`, `projects/demo-ds/crt0.s`; PowerPC (Gekko): `projects/demo-gc/build.py` (`-proc gekko`) | gcc-2.7.2-kmc, gcc-2.7.2-psx (MIPS); arm-gnu (ARM/Thumb); MWCC 2.0 (DS); MWCC GC 1.3.2 (GC); none for the Linux lane |

Real-toolchain lanes and the tests that use them: `projects/demo-psx` (`tests/test_psx.py`, `tests/test_overlays.py`), `projects/demo-n64` (`tests/test_phase23.py`, `tests/test_period_compilers.py`), `projects/demo-gba` (`tests/test_gba.py`), `projects/demo-ds` (`tests/test_ds_rom_check.py`). `tests/test_overlays.py` and `tests/test_ds_rom_check.py` are skipped unless the built EXE or ROM exists.

Holdout: functions never used to tune mutation or retrieval weights. Holdouts are read by the
retrieval leakage gate and gate retrieval surfacing; live-agent exclusion from tuning context is
planned for when the live-agent benchmark runs (not executed yet). A project declares them in two
ways, both read by the retrieval leakage gate (`decompnet.retrieval.feedback.load_holdout_answers`):
a `holdout.txt` in the project root (`projects/demo/holdout.txt`; one function name per line, `#`
comments allowed) and/or the
project config list `retrieval.holdout` (function names). With retrieval enabled, a function
listed in either whose target or source is found in the admitted corpus has its retrieval
candidates blocked.
