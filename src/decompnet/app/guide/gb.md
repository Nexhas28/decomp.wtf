# Matching Game Boy and Game Boy Color code

Game Boy (DMG) and Game Boy Color (CGB) cartridges run a Sharp **SM83** (LR35902) — a 4.19 MHz
8080/Z80 relative with its own instruction set. capstone has no Game Boy backend, so the fork carries
its own disassembler (`decompnet.rom.sm83`) and reads cartridges through `mgbdis`-style output.

## What the importer does with a .gb / .gbc file

- Detection is by content, not extension: the Nintendo logo at 0x104, a matching header checksum at
  0x14D, and a power-of-two size of 16 KiB banks (32 KiB … 8 MiB).
- The header gives the title, game code, CGB flag, cartridge type, ROM/RAM size and region.
- Function discovery splits the cartridge into **one segment per 16 KiB bank**: bank 0 lives at
  0x0000–0x3FFF and every other bank is switched into the 0x4000–0x7FFF window, which is what the
  hardware does. Automatic names are bank-scoped, so a function in bank 3 keeps its address inside
  that bank.

## The matching lane

Matching is done with the pinned **GBDK-2020** toolchain: `sdcc -msm83` for C, `sdasgb` for assembly.
`decomp setup` installs it (every download is pinned by sha256 per platform).

A Game Boy ROM's bytes contain **absolute** call and jump addresses, so the lane compiles in absolute
symbol mode: the build emits `.equ` lines that pin SDCC's references to the ROM's own addresses, and
scores the result with the same judge as every other platform.

**Until the toolchain is installed**, a `.gb`/`.gbc` project is imported **read-only**: every function
is disassembled, searchable and briefable, but functions are not handed out for matching, because no
candidate could be judged yet. That is deliberate — it is the honest state rather than distributing
work nobody can score. Run `decomp setup`, or ask the owner, to turn the lane on.

## What moves SDCC's output

The lane is new, so this list is short on purpose. Add what you measure with `add_note` (no function
name) so the next session starts ahead:

- Local declaration order and first use change register allocation — reorder before rewriting.
- Narrow types matter: SM83 arithmetic is 8-bit, and `u8`/`s8` locals versus `int` change the
  truncation SDCC emits.
- SDCC's default here is `--opt-code-speed`; if a function looks compiled for size, say so in a note.

## Conventions

- `bankNNN_XXXX` style automatic names, `D_`/`B_` for data by address; keep the split's names unless a
  function's purpose is clear.
- Helper types and prototypes that several functions share go in the project's shared header
  (`get_context`), never edited per function to force a match.
