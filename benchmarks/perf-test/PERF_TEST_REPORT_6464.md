# decomp.wtf fork — production (127.0.0.1:6464) perf-test report

Agent: perf-test · Run date: 16 Oct 2026 ~02:00–02:11 · Bounded run: 8 functions / 11 compiles / ~11 min wall.

## Sanity check
- `progress`: slus-01363-4 at 1210/1661 (50.7%) ✓ — matches expected baseline.
- `autopilot: done, 375/375 drafts tried, 0 matched` (receipt shown in progress).
- `working now` was NOT idle (contrary to brief): dozens of @GUMBO_SUPER_* agents, MrGoogle*, @autopilot, @permuter (holding func_800A6794, the app's own background search), plus sibling perf-* agents. @permuter holds one function persistently; never seen it move during the run.

## Per-function results
| function | insns | start best | compiles | final score | matched | wall | classification |
|---|---|---|---|---|---|---|---|
| func_8004B738 | 37 | 1355 | 1 | 3545 (mine); best stays 1355 | no | ~1 min | wall:constant-jalr (jalr through immediate 0xB0; ra restored from global; unused addiu t1,0x56) |
| func_8003680C | 113 | 240 | 4 | 1015 (mine); best stays 240 | no | ~5 min | stall — 4 focused edits (verbatim-best→400, s8→u8, -fno-schedule-insns, local x=0x80 hoist) no progress; stored best not reproducible |
| func_8003F1C0 | 67 | 15 | 0 | 15 | no | ~1 min | wall:mflo-v1/v0 regalloc tie (90 variants already swept; released unspent) |
| func_8003A2A8 | 61 | none | 0 | — | no | ~1 min | wall:boundary-fragment (live-in $s2 at entry, no prologue; needs boundary merge) |
| func_80037464 | 83 | 80 | 0 | 80 | no | ~1 min | wall:allocno-swap ($s2/$s3; 8 shapes exhausted, floor 80) |
| func_8003B2D0 | 33 | none | 1 | 925 (mine = new best) | no | ~2 min | wall:live-in-register (bare `ori $t0,$t0,0x3d3`; caller loads lui 0x1f80; m2c "unset register") |
| func_8003E32C | 43 | 240 | 4 | 1720 (mine); best stays 240 | no | ~4 min | wall:gp-relative-unreachable — target reads globals via %gp_rel; fork rebuild emits lui/lo absolute for ALL declaration styles (-G8, extern, tentative def, gcc-2.7.2 profile). Stored best 240 unreproducible |
| func_8003ED00 | 154 | 10 | 1 | 610 (mine); best stays 10 | no | ~2 min | wall:load-delay-nop / stored-best-stale — displayed best recompiles 610 (4 extra insns incl. nop after lw before j; known 2-row mflo tie was under old pipeline) |

## Totals
- Functions attempted: 8 (cap hit) · Compiles: 11/60 · Matches: 0 · Wall time: ~11 min
- Median compiles/function: 1 (per-fn compiles: 1,4,0,0,0,1,4,1)
- Median mcp round-trip latency: **~0.08 s** (compile_and_diff 0.08–0.09 s; claim 0.05–0.15 s; get 0.08–0.13 s; release ~0.05 s; open ~0.49 s; progress ~0.11 s; guide/context ~0.05 s). No mid-run outage.
- Biggest single-edit score drop achieved: **+615** (func_8003680C 400→1015 via local `u8 x=0x80` hoist edit). Vs stored best: +775 (240→1015). No single edit improved any score; 0 progress, 0 matches.
- Classification counts: match 0 · progress 0 · stall 1 · wall 7 (signatures: constant-jalr, mflo-regalloc-tie, boundary-fragment, allocno-swap, live-in-register, gp-relative-unreachable, load-delay-nop).

## Fork behaviors observed (production 6464)
1. **Claim identity collision (real, twice):** claims are keyed by agent name. A second agent sharing "perf-test" displaced my claims mid-work → `Error executing tool compile_and_diff: you hold another claim; release it before working on a different function`, and my 29-min lease effectively vanished (status showed "perf-test on func_8004CDE4" — a function I never claimed). Had to re-claim before diffing. A sibling agent's saved diff_5.txt shows the identical error — corroborated, not unique to me.
2. **Stored bests are stale on the NEW server — 3/3 do not reproduce.** Displayed "Best attempt so far" recompiles worse every time: 8003680C 240→400, 8003E32C 240→1720, 8003ED00 10→610. Root causes identified: (a) rebuild cannot emit %gp_rel small-data addressing (tested extern / tentative-def / context.h style / `-G8` cflag / gcc-2.7.2-psx profile — all lui/lo), while the ROM was built gp-relative; (b) rebuild inserts load-delay nops the ROM lacks (maspsx won't drop them). Consistent with the production restart/fork deployment (sibling CONFIRM.txt: "The restarts you heard about were production (6464) only").
3. **No ⚠ stale-context warnings** observed anywhere this session (grepped all saved outputs) — context.h v52 everywhere.
4. Claim lease = 29 min, renewed per compile; claiming a new function auto-releases the old one and records a "switching claim" note under the new function; one-claim-at-a-time enforced server-side.
5. The project-wide notes and every function's in-function notes show the C-only frontier is saturated (lead-side-only unblocks: boundary merge, overlay jtbl data, aspsx %hi sharing).

## Honest read on the fork's end-to-end performance (production)
Infrastructure: **fast and stable.** Sub-100 ms judge (compile+diff) round-trips through the local bridge to 127.0.0.1:6464, no downtime, large `open` listings ~0.5 s. Serving-side performance is excellent.
Decomp progress: **effectively zero on this pool.** Every sampled function carries prior wall notes; I added wall signatures (constant-jalr, live-in-register, gp-relative-unreachable, load-delay-nop) and demonstrated the stale-best problem. The "start from this best" workflow is currently misleading on the new server: the rebuild pipeline differs from the one that produced stored bests (gp-relative emission and load-delay nops), so many recorded bests are unreproducible until the pipeline matches the original toolchain. Recommend: lead-side fixes (gp-relative support, maspsx nop dropping, boundary merge) before more C-only sweeps, and unique agent names to avoid claim stomping.

Artifacts: /Users/shanemcgovern/Decomp/work/perf-test/{func_*,get_*}·c/txt (my files: func_8004B738_v1.c, func_8003680C_v1..v4.c, func_8003B2D0_v1.c, func_8003E32C_v1..v4.c, func_8003ED00_v1.c, get_*.txt). All claims released (status shows no perf-test entry).
