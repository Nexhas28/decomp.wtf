# Matching KMC GCC 2.7.2 (many third-party N64 games)

Flags are usually `-O2 -mips3 -G0` (some games `-O1` or `-O3`; ask the user to change the project's
compiler settings if nothing matches at all).

Traits
- Unconditional jumps inside a function are `j label`; IDO would use `b`. (That's how the app guessed.)
- `move` is written as `addu rd, rs, $zero`.
- Branch-likely (`beql`, `bnel`) is common in loops and simple ifs.
- The function's local variable declaration order affects register allocation and stack slots.
  Reorder declarations before rewriting logic.
- Expressions are evaluated mostly left to right; swapping operands of `+`, `==`, `&&` can change
  register use or instruction order.

Things to try when registers differ (`r`)
- Introduce or remove a temporary variable; reuse one variable for two values.
- Change `int` to `s16`/`u8` (or back) for locals and parameters; signedness changes comparisons and
  extension instructions.
- `for` vs `while` vs `do { } while` loops produce different block layouts. `do-while` has no
  initial check.
- Pre/post increment, `x += 1` vs `x++`, pointer walking (`*p++`) vs indexing (`p[i]`).
- Early `return` vs `if/else` with one return at the end.
- `if (a) { if (b) ... }` vs `if (a && b)`.

Calls and prototypes
- Without a prototype, GCC assumes `int f()` and promotes arguments: floats become doubles,
  `s16`/`u8` arguments are not truncated. Missing extensions or `cvt.d.s` before calls point at
  prototype problems: add the prototype (edit_context).

switch
- Dense cases become a jump table (`jtbl_` in .rodata, `jr $reg`); sparse ones become compare chains.
  Your jump table must equal the ROM's, so case values and order matter.

## Worked examples (typical; one change, then recompile)
Loop counter in the wrong register (`r` on every loop line): declare it first, or pin the hot one.
```
r  addu $v1,$v1,$a0  vs  addu $v0,$v0,$a0      int i; s32 sum;  ->  s32 sum; int i;
```
Sign extension (`|` sll/sra vs nothing, or andi): the target keeps a 16-bit value.
```
| sll $v0,$a0,16 / sra $v0,$v0,16  vs  (none)     int x = p->hp;  ->  s16 x = p->hp;
```
Extra `sw`/`lw` pair around a call (`>`): the target keeps the value in a saved register, so mimic that with a temporary.
```
> sw $v0,0x18($sp) ... > lw $v0,0x18($sp)         f(g(a), g(a));  ->  t = g(a); f(t, t);
```
Magic multiply in the target (`lui 0x6666` / `mult`): the original is plain division.
```
< lui $at,0x6666 / mult ...   vs  yours: div     x / 10  (use s32; u32 gives a different sequence)
```
Missing branch-likely or loop shape (`<` `beql`, `|` at loop top): try `do { } while` or a `for` with a different test.
```
| beql $v0,$zero,L  vs  beq + nop                for (i = 0; i < n; i++)  ->  i = 0; do { ... } while (++i < n);
```
