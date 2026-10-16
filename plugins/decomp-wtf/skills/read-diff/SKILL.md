---
name: read-diff
description: Read a decomp.wtf compile_and_diff result and choose the single next change — score, diff markers, "NOT A MATCH YET", assembler-variant and linking notes, and the "🧭 Next action" advice. Use after every compile_and_diff that is not a match.
---

# Reading a compile_and_diff result

## The head line
`score S (P% match) — best so far B. Target X insns, yours Y.` Score 0 is a match; 0% is 100 per target instruction, and
extra instructions can push the score past that. Compare X and Y first: a different instruction count means the structure is still wrong, and
nothing else is worth fixing yet.

## The diff
Only mismatching rows are shown, with 2 rows of context, `...` between gaps; long runs of missing or extra rows are
shortened, and past about 60 lines it stops with "fix these first". The counts line above it and the
`mismatch summary` line below it count the same mismatches (the summary counts a `|` as one missing plus one extra).

| Marker | Means | Penalty |
|---|---|---|
| `<` | missing: the target has it, yours doesn't | 100 |
| `>` | extra: yours has it, the target doesn't | 100 |
| `\|` | a different instruction in the same place | as one missing + one extra |
| `o` | the same instruction in a different place (two `o` rows per move) | 60 per move |
| `i` | the same instruction with a different operand (constant, offset, symbol) | 10 |
| `r` | the same instruction with different registers | 5 |
| `s` | a different stack offset | 1 |

**Fix in this order, one change per compile:** `<` `>` `|` (control flow, calls, loops, missing statements, wrong
types that change instructions) → `o` (statement order, where a value is computed) → `i` (constants, struct offsets,
types and signedness) → `s` (locals, their types and order) → `r` last (expression shape, temporaries, the order
variables are first used). Registers are the compiler's reaction to everything else: they often fix themselves once
the structure matches.

## Lines after the diff
- `NOT A MATCH YET: every instruction reads the same, but the bytes differ` → follow the linking notes below it, or
  the difference is invisible in the listing: a branch form, an immediate or a relocation.
- `when linking your code at the function's address:` → each line is a reference that doesn't land where the game's
  does (usually a symbol or a data declaration): fix those.
- `assembler variant: …` → the app tried another assembler pipeline for you and kept it because it scored better.
  This is automatic; nothing to do.
- `compiler output:` → warnings. An implicit declaration means a missing prototype, which changes argument and
  return handling: declare it in your own source now, and move it to the shared header after the match
  (`share-knowledge`).

## "🧭 Next action" (appears once you are stalled)
Check it against the diff yourself: the advice ignores `i` rows and some functions' history.
- **PERMUTATION** → no rows are missing, extra or moved. If `i` rows remain, fix those first (constants, offsets,
  types). Once every remaining row is `r` or `s`, release with "only register differences left" (see `match-loop`).
  The permuter takes unclaimed functions of 400 instructions or fewer whose best score is at most 6 per instruction;
  it starts when helper agents are started and stops itself after about an hour idle. With no helpers, nothing
  permutes: your notes are what the next agent gets.
- **TARGETED INVESTIGATION** → `escalate_function(function, question)` once. It needs Ghidra on the user's computer
  and says plainly when no analysis provider is available: then release instead of retrying.
- **RELEASE** → release with notes.
- **M2C DRAFT** → ignore it (no tool makes one; it hides the other advice on functions without a draft). Read the diff:
  only `r`/`s` rows left means PERMUTATION, otherwise RELEASE.
Advice marked "historical attempt" was computed from a stale snapshot: recompile before trusting it.
