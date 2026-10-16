#!/usr/bin/env bash
# Smoke test for pygfxd: disassemble a hand-made F3DEX2 display list and check the macros.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Two 8-byte big-endian Gfx words: G_RDPPIPESYNC (0xE7) then G_ENDDL (0xDF for F3DEX2).
printf '\xe7\x00\x00\x00\x00\x00\x00\x00\xdf\x00\x00\x00\x00\x00\x00\x00' > "$WORK/dl.bin"

OUT="$("$DIR/run" "$WORK/dl.bin" f3dex2)"
echo "$OUT"

grep -q 'gsDPPipeSync()' <<<"$OUT"
grep -q 'gsSPEndDisplayList()' <<<"$OUT"
echo "PASS: pygfxd disassembled gsDPPipeSync and gsSPEndDisplayList"
