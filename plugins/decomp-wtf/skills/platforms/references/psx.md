# PlayStation 1: what the `psx` guide doesn't say

Measured on a real PSY-Q project (benchmarks/perf-test).

- **Assembler variants run by themselves.** Near misses (score 300 or less, or 5+ extra instructions) and failed compiles
  are retried with the maspsx preprocessor and a modern-binutils pass; a better result is kept and the reply says
  `assembler variant: …` (for a rescued failed compile, the variant line is under `compiler output:`). You can't pick a variant, and you don't need to.
- **Small globals ($gp-relative).** The ROM reads a small global through `%gp_rel` only when the global sits in
  `.sdata`, i.e. is defined **with an initializer** (`s32 D_80062718 = 0;`). A tentative definition (`s32 D_x;`, as
  the `psx` guide shows) lands in `.bss` and compiles to a full `lui`/`%lo` address. A project-wide header change
  to initializers was refused by the regression gate (it broke two matched functions), so apply it per function, and
  check the diff. Even in `.sdata`, `&X` compiles to `%gp_rel` while the ROM may build the address with
  `lui/addiu`: then it's a wall.
- **Jump tables the assembler rejects** (`Illegal operands lui/addiu` on `jtbl_` labels) can be bypassed in C with a
  computed goto through the extern table: `extern void *jtbl_X[];` then `goto *jtbl_X[n];`, with `&&label`
  references keeping each case label alive. Measured on one function: it compiles and diffs; the remaining differences were usually
  $gp and load-delay walls.
- **Walls seen often:** GTE/COP2 instructions (`lwc2`, `ctc2`, …), the load-delay `nop` and BIOS call veneers: name
  them as such in release notes. Register-only ties after the structure matches are permuter work, not walls:
  release those as "only register differences left" (see `match-loop`).
