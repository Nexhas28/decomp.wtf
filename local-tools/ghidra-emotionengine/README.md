# ghidra-emotionengine (Ghidra Emotion Engine: Reloaded)

Ghidra extension adding PlayStation 2 support: the MIPS-R5900 (Emotion Engine) language with MMI / VU0 macro / COP instructions, a STABS analyzer for `.mdebug` ELFs, PCSX2 save-state import and a constant-reference analyzer.
Useful for decomp.wtf because PS2 projects need EE-specific instructions (lq/sq, paddw, ...) to disassemble and decompile correctly before matching with ee-gcc / mwcc-ps2.

- Install: the release zip `ghidra_12.1.4_PUBLIC_20261004_ghidra-emotionengine-reloaded.zip` in this folder, from https://github.com/chaoticgd/ghidra-emotionengine-reloaded release v2.1.38 (commit fcc5deac), unmodified.
- Use: in Ghidra 12.1.4 use File > Install Extensions and pick the zip, or unzip it into `~/Library/ghidra/ghidra_12.1.4_PUBLIC/Extensions/`. Headless: `analyzeHeadless <proj> p -import game.elf` (PS2 ELFs auto-select `r5900:LE:32:default`) or `-processor r5900:LE:32:default` for raw blobs.
- Needs Java 21 (`/opt/homebrew/opt/openjdk@21`).
- Test: `bash test.sh` installs the zip into a throwaway user.home, imports a hand-made 20-byte R5900 blob with analyzeHeadless and checks `lq`, `paddw` and `sq` disassemble. Result: pass (2026-10-16).
