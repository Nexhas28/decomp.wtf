# decomp.wtf fork — Perf-Test Run 2 Report (test instance :19388, slus-01363-4)

Agent: perf-test/perf-r3 via tools/mcp_agent.py. Window: ~57 min. Bounds respected: 8 functions, 17 compiles (≤60), no probe/snipe used.

## Per-function table

| Function | insns | start best | compiles | final score (mine) | matched? | class | notes |
|---|---|---|---|---|---|---|---|
| func_8004B65C | 54 | 4550 | 1 | 4550 | no | wall: jr-tail-dispatch | mid-fn `jr $ra`; 28 unreachable tail insns (0xDFFC vector, KSEG1 hw calls 0xA000DFAC/80); m2c drops post-jr blocks; 13/54 insns |
| func_8004B738 | 37 | 1355 (STALE) | 2 | 2845 | no | wall: toolchain-floor | stored 1355 has NO attempt row (unreproducible); floor 2845 = same source. ra-in-global epilogue, BIOS `jalr t2` veneer, load-delay nops, 0xDFFC store needs ROM symbol that doesn't exist |
| func_800A2D38 | 1594 | 159265 | 0 | — | no | (closest probe, unspent) | `claim closest` now hands a 1594-insn giant; released unspent |
| func_8004CDE4 | 71 | 780 (STALE) | 3 | 1320 | no | stall | stored 780 unreproducible; v52 floor 1320 (charizard shape re-verified). Residual lead-side: symbol-%lo folding vs explicit lui+addiu base, shared-epilogue j vs jr tail-merge, base regalloc v1 vs a3 |
| func_80057A0C | 43 | 2975 (STALE) | 3 | 3375 | no | stall | draft re-diffs 3380 today. Fixed BIOS-ptr deref + do-while compare loop (3380→3425→3375). Dead `addiu t1,0x56` target artifact; ra-in-global; -O2 reordering vs unscheduled target |
| func_8004CB78 | 150 | 6147 (reproducible) | 4 | 6327 | no | stall | raw m2c draft (6147) BEATS all clean rewrites (7068/6327/6588/7110). m2c's 12-arg fake signature reproduces the 0x90 frame layout better than a correct 6-arg signature — judge rewards frame-accidental shapes |
| func_800574F0 | 34 | 3200 | 1 | 3200 | no | wall: GTE/COP2 | `ctc2` to COP2 ctrl regs 0-7/24/25/26, 4 exit points; no C compiles to ctc2 under gcc-2.8.1 (m2c emits 2 insns) |
| func_800B0570 | 90 | 2405 (reproducible) | 3 | 2421 | no | stall | stored draft re-diffs 2421; hoist-cast edits regressed (2797/2630). OT-drawer family coloring wall (s2/s3/s4/s5/s6 assignment, s16<<16 hoist form) |

## Totals
- Functions attempted: 8 · Compiles used: 17 (16 mine + 1 foreign attributed to my id) · Matches: 0 · Progress (beat stored best): 0
- Wall time (bridge seconds spent on wall functions): ≈5.3s (B65C ~3.3s, B738 ~1.0s, 574F0 ~1.0s)
- Median compiles/function: 2.5 · Median round-trip: claim-by-name ~0.93s, diff ~0.108s (min 0.070, max 0.129), release ~0.30s, open/progress/info 0.07–0.22s
- Best score reached this run: 1320 (CDE4, re-verifying charizard's stored shape). Best score from a source I newly wrote: 1560 (CDE4 v2)
- Biggest single-edit score drop: NONE positive vs stored bests (every edit worsened or stayed above the stored best; all stored bests that look beatable are stale/unreachable). Largest within-run delta: CB78 7068→6327 (−741)
- Classification counts: match 0 · progress 0 · stall 4 · wall 3 (jr-tail-dispatch 1, toolchain-floor 1, GTE/COP2 1) · unspent 1

## Fork behaviors observed (operational findings)
1. **Agent identity split-brain (reproduced)**: reusing a bridge name across daemon restarts creates suffixed agent rows (perf-test id 847 / perf-test_2 id 861; perf-r3 / perf-r3_2). The same daemon flips between rows at reconnect boundaries. Symptom: `release_function` → "you don't hold a claim" while `get_function` says "claimed by you" (hit twice on func_8004B65C). Fix: `mcp_agent.py --name <name> stop` then any command — the fresh connection re-resolves to the original row and release succeeds. Claims made under the old row are unreleasable from the new one until then.
2. **Background actor under my identity**: attempt 13263 (a volatile-dropped variant of my B738 source, score 3545) was recorded under my agent id 24s after my diff, and my B738 claim was silently released ~90s later. The app log shows an active trycloudflare collab room ("slus-01363") — collab/permuter activity rides the claim holder's identity and can revoke claims mid-iteration.
3. **Stale stored bests are pervasive**: B738 1355 (no attempt row exists), CDE4 780 (floor 1320 today), 57A0C 2975 (draft re-diffs 3380). Briefs hand out the stale best as "start from this" with no context-version warning on the brief itself.
4. **`claim closest` drifted**: now returns a 1594-insn giant (best 159265), not Run 1's near-miss walls — the small near-miss pool has been swept by the GUMBO fleet.
5. **probe/snipe confirmed no-op**: server compile_and_diff takes only {function, source}; the "decomp-compiler/cflags" header comments are client-side fiction (fixed project toolchain gcc 2.8.1 -O2 -G8).
6. **Judge responsiveness is excellent**: ~0.1s per diff round-trip; briefs (claim/get) ~0.7–1.2s; no judge queueing observed even with 36+ fleet workers active.
7. **Fleet saturation is real**: every open function carries GUMBO wall notes; autopilot 461/461 = 0 matched per notes; the fleet is still landing near-misses (score 100s) during this run. Claim availability by name is fine (medium functions open), but every claimable function is a documented wall/stall — no green slope exists on the C side with the fixed toolchain.
8. **m2c beats clean C**: on func_8004CB78 the raw m2c draft (6147) outperformed all 4 clean rewrites; the judge is sensitive to frame-accidental shapes.
