# Matching GBA code built with agbcc

agbcc is pret's rebuild of the GCC 2.95-era compiler most GBA games used (`old_agbcc` for some early
games, `agbcc_arm` for ARM code). Typical flags `-mthumb-interwork -O2`; the project sets them per file.

What moves the output
- Local declaration order and first use change register allocation. Reorder before rewriting.
- Narrow types: every `u8`/`s16` local or parameter gets truncation shifts after arithmetic; `int`
  locals don't. Match the shifts in the target by choosing types.
- Loop shape: `for`, `while` and `do {} while` produce different block layouts.
- `if (a) { if (b) }` vs `if (a && b)`, early returns vs one exit.
- Temporaries: introduce or remove one; reuse a variable for two values.
- Casts like `(u16)`/`(s32)` placed on subexpressions change when truncation happens.
- `register u32 x asm("r4")` pins a variable to a register; pret projects use it sparingly where nothing
  else works. Prefer natural code first.

Project conventions (pret-style): `gSomething` globals, `sSomething` statics, `Task_*` callbacks,
`FAKEMATCH`/`NONMATCHING` markers for hacks. Look at how matched files in the project write similar code
before inventing a trick.

Calls into BIOS use `swi`; the project wraps them (`CpuSet`, `LZ77UnCompVram`...): use the wrappers The
wrappers themselves (`swi n; bx lr`) are set aside as BIOS code: you won't be handed one.

## Worked examples (typical for agbcc -O2; one change, then recompile)
Extra `lsl`/`lsr` pair after arithmetic (`>` lsl r0,#16 / lsr r0,#16): a narrow local is truncating; the target keeps `int`.
```
> lsl r0,r0,#16 / > lsr r0,r0,#16          u16 sum;  ->  s32 sum;
```
Missing truncation (`<` lsl/asr #24): the target stores into an `s8`/`u8` and reloads it.
```
< lsl r0,r0,#24 / < asr r0,r0,#24          s32 v = x;  ->  s8 v = x;
```
Loop counter in `r4` vs `r5` (`r` across the loop): declaration or first-use order.
```
r add r5,#1  vs  add r4,#1                  int i; int n;  ->  int n; int i;
```
Extra `mov` copying a parameter (`>` mov r4,r0): the target uses the parameter directly; remove the local copy.
```
> mov r4,r0                                 s32 p = arg; use(p);  ->  use(arg);
```
Condition inverted (`|` bne vs beq with swapped blocks): early return vs one exit.
```
| beq L1  vs  bne L1                        if (a) { body } return;  ->  if (!a) return; body;
```
