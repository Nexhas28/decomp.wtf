---
type: Tool
title: GhidraBoy
description: Open-source Ghidra extension adding Sharp SM83 (Game Boy) language support and a Game Boy ROM loader.
resource: https://github.com/Gekkio/GhidraBoy
tags: [game-boy, sm83, ghidra-extension]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-16T12:03:23Z }
verified:
  - { by: process:local-tools-smoke-test, at: 2026-10-16T12:03:23Z }
sources:
  - id: repo
    resource: https://github.com/Gekkio/GhidraBoy
    title: GhidraBoy README
---

# GhidraBoy

GhidraBoy adds the Sharp SM83 CPU (used in the Game Boy) to Ghidra, plus a ROM loader that sets up the Game Boy memory map and names the 0xFF00 hardware registers [^repo]. This gives Ghidra correct disassembly and decompiler input for Game Boy code, which is the starting point for a byte-matching Game Boy decompilation. The upstream project is archived and targets Ghidra 11.x; the local copy has been ported to Ghidra 12.1.4.

## Local install

Path `local-tools/ghidraboy/`, upstream release 20250830 (GhidraBoy-1.7.1) with a local Ghidra 12.1.4 port (`ghidra-12.1-port.patch`). Entry point: the extension zip `ghidra_12.1.4_PUBLIC_20261016_GhidraBoy.zip`, unpacked into a Ghidra user-extensions folder. Runs natively on macOS arm64 with Java 21.

## Examples

```bash
bash local-tools/ghidraboy/test.sh
```

The test core is an `analyzeHeadless` import with the Game Boy loader:

```bash
"$GHIDRA/support/analyzeHeadless" "$W/proj" p -import rom.gb -loader GameBoyLoader \
   -scriptPath "$W/scripts" -postScript Dump.java -noanalysis -deleteProject
```

## Test result

Pass, 2026-10-16. `test.sh` builds a small rgbds ROM and checks that the language is `SM83:LE:16:default`, the `rom@0000` block exists, and the instructions at 0x150 are `LD A,0x2a`, `LD (0xc000),A`, `HALT`.

## Related

- [Game Boy toolchain and project notes](/project/toolchains.md)

[^repo]: GhidraBoy README (https://github.com/Gekkio/GhidraBoy)
