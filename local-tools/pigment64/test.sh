#!/usr/bin/env bash
# Smoke test: make a 4x2 RGBA PNG (pure Python, no deps), convert it to N64 RGBA16
# with pigment64, check exact bytes, convert back to PNG and back to bin again (round trip).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
G="$HERE/bin/pigment64"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cd "$T"
python3 -I - <<'PY'
import struct, zlib
w, h = 4, 2
px = [(255,0,0,255),(0,255,0,255),(0,0,255,255),(255,255,255,0)] * 2
raw = b"".join(b"\x00" + b"".join(bytes(p) for p in px[y*w:(y+1)*w]) for y in range(h))
def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) \
    + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
open("in.png", "wb").write(png)
PY
"$G" to-bin -f rgba16 -o out.bin in.png
hex=$(xxd -p out.bin | tr -d '\n'); echo "rgba16: $hex"
# RGBA5551 big-endian: red=f801 green=07c1 blue=003f white-transparent=fffe
[ "$hex" = "f80107c1003ffffef80107c1003ffffe" ] || { echo "FAIL rgba16 bytes"; exit 1; }
"$G" to-png --width 4 --height 2 -f rgba16 -o back.png out.bin
[ "$(head -c4 back.png | tail -c3)" = "PNG" ] || { echo "FAIL png"; exit 1; }
"$G" to-bin -f rgba16 -o again.bin back.png
cmp out.bin again.bin
"$G" to-bin -f i8 -o i8.bin in.png
[ "$(wc -c < i8.bin | tr -d ' ')" = "8" ] || { echo "FAIL i8 size"; exit 1; }
echo PASS
