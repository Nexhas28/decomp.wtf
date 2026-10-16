#!/usr/bin/env bash
# Smoke test: disassemble a hand-made F3DEX2 display list (hex on the command line and
# a binary file on disk) and a classic F3D one; check the decoded gbi.h macros.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
out=$("$HERE/bin/gfxdis.f3dex2" -d E700000000000000DE00000006001230DF00000000000000)
echo "$out"
grep -q 'gsDPPipeSync()' <<<"$out"
grep -q 'gsSPDisplayList(0x06001230)' <<<"$out"
grep -q 'gsSPEndDisplayList()' <<<"$out"
printf '\xe7\0\0\0\0\0\0\0\xdf\0\0\0\0\0\0\0' > "$T/dl.bin"
"$HERE/bin/gfxdis.f3dex2" -f "$T/dl.bin" | grep -q 'gsSPEndDisplayList()'
# classic Fast3D uses a different opcode (0xB8) for end-of-list
"$HERE/bin/gfxdis.f3d" -d B800000000000000 | grep -q 'gsSPEndDisplayList()'
echo PASS
