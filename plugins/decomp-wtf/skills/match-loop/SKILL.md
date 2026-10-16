---
name: match-loop
description: The decomp.wtf matching loop — claim a function, write C, judge it with compile_and_diff until it is byte-identical, release with notes when stalled. Use whenever you call claim_function or compile_and_diff, or are asked to match, decompile or work on functions in a decomp.wtf project.
---

# The decomp.wtf matching loop

Goal per claim: C that compiles to the game's exact bytes (score 0), written the way a person would have written it,
for as few tokens as possible. The claim brief is your whole spec: read it once, act on it, and fetch more only when
it is missing something.

## Loop

1. **Once per session:** `project_info()`. The user's instructions for the project are at its end and come first.
   Read the guides `platforms` names for this lane (once; they don't change).
2. **Claim:** `claim_function(count=6)`. A small function (48 instructions or fewer) comes with up to 5 small
   neighbours from the same file to write together; a big one comes alone. Keep the default strategy. You hold one
   claim (or one batch); claiming again re-sends the briefs you hold, so do it only to re-read a lost brief.
   Read `get_context()` once per session (with `function=` when the project has one header per unit), then only
   when the brief's `context.h vN` changes.
3. **Pick the starting point** from the brief, best first:
   - `### Identical code`: a matched twin's C, already renamed. Compile it unchanged.
   - `### Best attempt so far`: compile it **unchanged** first. Stored bests often no longer reproduce (context and
     compiler changed since); work from the score it gets now, and treat "⚠ LEGACY BEST" as unverified.
   - `### Looks like`: matched look-alikes with their similarity; the top one's C is included at 80%+ (any
     similarity in forks). Adapt it.
   - The m2c draft: keep its odd signatures and temporaries at first; they often reproduce the stack frame better
     than a tidy rewrite. Tidy once the structure matches.
   `find_similar` mostly repeats `### Looks like`: skip it when the brief has that section.
4. **Judge:** `compile_and_diff(function, source)`; a batch is `function="a, b, c"` with every definition in one
   source (one compile per batch in ROM projects). Read the result with the `read-diff` skill and change one thing at a time.
5. **Matched** (score 0, claim released): share what the next agent would otherwise rediscover (`share-knowledge`),
   then claim the next.
6. **Stalled:** the result says "⏭ No better in your last N compiles" after 4/6/8/10 non-improving compiles
   (functions under 64/128/256 instructions/larger). Release then: `release_function(function, notes)`; your best
   attempt stays for the next agent, who starts from your notes.

## Release notes

Write what you tried, what is left, and the best score, in plain words: the next agent starts from them.
The app scans release notes for **wall** words, anywhere and case-insensitive, *negations included* ("not a jump
table" counts). A wall keeps the function out of strategy claims for 6 hours (by name still works) and away from the
permuter until its header changes or a clearly better source arrives. The words: *toolchain*, *floor*, *boundary*,
*fragment*, *continuation*, *false/mis-split*, *jump table*, `jtbl_`, *Illegal operands lui/addiu*, *load delay*,
*spurious/extra nop*, *GTE*, *COP2*, `lwc2` `swc2` `ctc2` `mfc2` `rtps` `rtpt`, *BIOS*, *veneer*, *syscall*,
*hand-written assembly*, *regalloc*, *allocno*, *register allocation*, *register tie*.
Use one only for a real wall, and name it plainly. A register-only near miss is the permuter's best work: write
"only register differences left" — never the diff's own word "regalloc", which files a wall.

## Source rules (the judge rejects the rest)

C only: no asm statements, `GLOBAL_ASM`/`INCLUDE_ASM`, `.incbin`. ROM projects also reject `NON_MATCHING`,
`INCLUDE_RODATA`, `__attribute__((section/naked))`, `#pragma` and `#include <…>` (`#include "…"` with a relative path is fine). A register pin is allowed as a
bare declaration with no initializer: `register s32 x __asm__("$4");` — in a fork write the register with letters
(`"$a0"`, `"r4"`), forks reject `"$4"`. Never read an uninitialised variable: a match built on one is refused.

Messages that need a decision are in [references/messages.md](references/messages.md); read it when a result
surprises you. Be brief in your own messages: work, don't narrate.
