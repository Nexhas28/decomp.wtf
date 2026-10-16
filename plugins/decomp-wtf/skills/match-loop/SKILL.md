---
name: match-loop
description: The decomp.wtf matching loop. Always read this before calling claim_function or compile_and_diff, or when asked to match, decompile or work on functions in a decomp.wtf project.
---

# The matching loop

Goal: C that compiles to the game's exact bytes (score 0), written the way a person would write it.
The claim brief is your spec. Be brief in your own messages: work, don't narrate.

## Loop

1. **Once per session:** `project_info()` (the user's instructions at its end come first), `get_context()`, and the
   guides for your lane (`platforms` skill lists them).
2. **Claim:** `claim_function(count=6)`. Keep the default strategy. Claiming again only re-sends your briefs.
3. **Start from the brief**, best first: `### Identical code` (compile unchanged) → `### Best attempt so far`
   (compile unchanged, work from the score it gets now) → `### Looks like` (adapt the top one) → the m2c draft
   (keep its odd types and temporaries until the structure matches).
4. **Compile:** `compile_and_diff(function, source)`; a batch is `function="a, b, c"` with all definitions in one
   source. Triage the result with the table in `read-diff`.
5. **One change per compile.** Make exactly one edit the diff points to, compile, keep it only if the score drops.
   Never try several ideas at once and never loop on blind variations: if you have no specific idea from a marker,
   release.
6. **Matched:** claim the next one. If the match taught you a type, prototype or name, see `share-knowledge`.
7. **Stalled:** when the result says "⏭ No better in your last N compiles", stop and
   `release_function(function, notes)`. Notes: what you tried, what is left, best score. Your best attempt stays
   for the next agent.

## Release-note words

Notes are scanned (case-insensitive, negations included) for wall words that bench the function for 6 hours:
*toolchain, floor, boundary, fragment, continuation, mis-split, jump table, jtbl_, load delay, spurious nop, GTE,
COP2, BIOS, veneer, syscall, hand-written assembly, regalloc, allocno, register allocation*. Use one only for a real
wall. For a register-only near miss write "only register differences left": the permuter takes it from there.

## Source rules

C only: no asm statements, `GLOBAL_ASM`/`INCLUDE_ASM`, `.incbin`. ROM projects also reject `NON_MATCHING`,
`#pragma`, `__attribute__((section/naked))`, `#include <…>`. A register pin is a bare declaration:
`register s32 x __asm__("$4");` (forks: `"$a0"`/`"r4"`, never `"$4"`). Never read an uninitialised variable.

Odd result messages: [references/messages.md](references/messages.md).
