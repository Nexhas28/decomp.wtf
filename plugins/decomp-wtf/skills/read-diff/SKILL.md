---
name: read-diff
description: Triage a decomp.wtf compile_and_diff result that is not a match: instruction counts, diff markers, linking notes, "Next action" advice. Read it after a compile that did not score 0 and you are unsure what to change next.
---

# Triage a result

1. **Counts first.** Head line: `Target X insns, yours Y`. If X ≠ Y the structure is wrong; ignore everything but
   `<` `>` `|` until they match.
2. **Pick the first marker present** in this order and make the one change shown.

| Marker | Means | The one next change |
|---|---|---|
| `<` | target has it, you don't | add the missing statement, call or branch |
| `>` | you have it, target doesn't | remove or fold that statement; check for an extra temporary |
| `\|` | different instruction, same place | fix type, signedness or operator (`int`→`s16`, `<`→`>=`) |
| `o` | right instruction, wrong place | reorder statements or move where the value is computed |
| `i` | constant, offset or symbol differs | fix the constant, struct offset, type size or symbol |
| `s` | stack offset differs | reorder or retype locals |
| `r` | registers differ | reorder declarations, add/remove a temporary, change loop shape |

`r` is the compiler's reaction to everything above: it often fixes itself once the rest matches. Only `r`/`s`
left → release with "only register differences left".

## Lines after the diff
- `NOT A MATCH YET: every instruction reads the same` → follow the linking notes below it, or it is a branch
  form, immediate or relocation invisible in the listing.
- `when linking your code…` → a symbol or data declaration that doesn't land where the game's does: fix first.
- `assembler variant: …` → automatic, nothing to do.
- `compiler output:` implicit declaration → declare the prototype in your source (missing prototypes change
  argument handling); share it after the match.

## "🧭 Next action" (once stalled)
Check it against the diff; it ignores `i` rows.
- **PERMUTATION** → fix any `i` rows, then release as above.
- **TARGETED INVESTIGATION** → `escalate_function(function, question)` once; if no analysis tool, release.
- **RELEASE** → release with notes. **M2C DRAFT** → ignore; read the diff.
"historical attempt" advice is stale: recompile first.
