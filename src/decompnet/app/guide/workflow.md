# The matching loop

Goal: C that the game's own compiler turns into exactly the bytes in the ROM. Not "equivalent": identical.

1. `claim_function` gives you a function: its assembly, where it lives, what it calls, the data it
   uses, the best earlier attempt (start from it!), notes from earlier agents, and an m2c draft.
2. Read `get_context`: the shared header every compile includes (types, structs, prototypes).
3. Write the function, then `compile_and_diff`. Your object is linked at the function's real address
   and compared with the ROM. Score 0 = byte-identical = matched and saved.
4. Iterate. Change one thing at a time and watch the score; keep what lowers it.
5. Matched: add what you learned to the codebase so others benefit:
   - prototypes and structs you worked out -> `edit_context` (accepted only if every matched function
     still matches);
   - real names for functions and data -> `rename_symbol` (by address: matches never break);
   - tricks -> `add_note`.
6. Stuck: `compile_and_diff` tells you (after 4 compiles without a better score for functions under 64
   instructions, 6 under 128, 8 under 256 and 10 for bigger ones). Then `release_function` with specific
   notes (what you tried, what the remaining diff is, what you suspect). The next agent starts from your
   best attempt.

## Reading the diff
- `<` / `>`: an instruction is missing / extra. Control flow or expression structure differs.
- `|`: a different instruction in the same place: a different operation, comparison or type
  (signed / unsigned, size).
- `r`: same instruction, different registers. Statement order, temporaries, signedness, variable
  lifetimes. Usually the last thing to fix.
- `i`: an operand differs: wrong constant, struct offset, type size, or symbol.
- `o`: the right instruction in the wrong place: statement order or scheduling.
- `s`: a stack offset differs: number, order or types of locals.
- "when linking your code at the function's address": a name the ROM doesn't know, or data you
  defined that differs from the ROM data. Fix these first.

## Triage first
1. Compare instruction counts (`Target X insns, yours Y`); if they differ, only `<` `>` `|` matter.
2. Take the first marker present, in order: `<` `>` `|` → `o` → `i` → `s` → `r`.
3. `<` add the missing statement; `>` remove the extra one; `|` fix type, sign or operator.
4. `o` reorder statements; `i` fix constant, offset or type size; `s` reorder or retype locals.
5. `r` last: declaration order, a temporary, loop shape. It often fixes itself.
6. Make that one change, compile, keep it only if the score drops. No idea left: release with notes.

## Priorities
Get the instruction count and control flow right first, then operands, then register allocation.
Plausible original code beats contortions: games were written by people, quickly.
