# Game Boy / Color: what the `gb` guide doesn't say

- **Names** are `func_BBB_xxxx`: the bank in decimal, then the CPU address (e.g. `func_002_46c3`). Several banks map
  to the same CPU addresses, so always name functions; the brief's examples like `func_000b58` use an older scheme.
- Functions are claimable only if the GBDK-2020 toolchain was installed **before the import**: a project imported
  without it stays read-only (functions show as assembly). Install it (`decomp setup`), delete the project in the
  app and import again.
- Don't `rename_symbol` functions here: the rename matches by CPU address, which other banks share.
- Every claim brief carries the lane's notes (calling convention, volatile I/O pointers, calls by name, tail calls
  becoming `jp`): follow them, they are the spec here.
- There is no m2c draft, no undefined-behaviour guard and no `escalate_function` on this lane: the target assembly
  is all you have, so read it closely before the first compile.
