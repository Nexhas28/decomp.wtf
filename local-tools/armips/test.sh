#!/usr/bin/env bash
# Smoke test: assemble the same tiny MIPS routine for N64 (big-endian) and PSX (little-endian)
# and check the exact output bytes.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cat > "$T/n64.asm" <<'ASM'
.n64
.create "n64.bin", 0x80000400
func:
  addiu v0, a0, 1
  jr ra
  nop
.close
ASM
cat > "$T/psx.asm" <<'ASM'
.psx
.create "psx.bin", 0x80010000
func:
  addiu v0, a0, 1
  jr ra
  nop
.close
ASM
cd "$T"
"$HERE/bin/armips" n64.asm
"$HERE/bin/armips" psx.asm
n64=$(xxd -p n64.bin | tr -d '\n'); psx=$(xxd -p psx.bin | tr -d '\n')
echo "n64: $n64"; echo "psx: $psx"
[ "$n64" = "2482000103e0000800000000" ] || { echo "FAIL n64 bytes"; exit 1; }
[ "$psx" = "010082240800e00300000000" ] || { echo "FAIL psx bytes"; exit 1; }
echo PASS
