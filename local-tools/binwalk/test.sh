#!/usr/bin/env bash
# Smoke test: binwalk v3 finds and extracts gzip/xz streams embedded at offsets in a hand-made blob, and a zlib file.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; BW="$HERE/bin/binwalk"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
python3 -I - "$T" <<'PY'
import gzip, zlib, lzma, sys, os
d = sys.argv[1]
g = gzip.compress(b'GZ_PAYLOAD ' * 50, mtime=0)
x = lzma.compress(b'XZ_PAYLOAD ' * 40)
open(os.path.join(d, 'blob.bin'), 'wb').write(b'\xAA' * 0x400 + g + b'\x00' * (0x800 - 0x400 - len(g)) + x + b'\xBB' * 0x80)
open(os.path.join(d, 'z.bin'), 'wb').write(zlib.compress(b'ZLIB_PAYLOAD ' * 40))
PY
cd "$T"
"$BW" -e -C out blob.bin > scan.txt 2>&1
grep -q '0x400 .*gzip compressed data' scan.txt || { cat scan.txt; echo "FAIL: gzip not found at 0x400"; exit 1; }
grep -q '0x800 .*XZ compressed data' scan.txt   || { cat scan.txt; echo "FAIL: xz not found at 0x800"; exit 1; }
grep -q 'GZ_PAYLOAD' out/blob.bin.extracted/400/* || { echo "FAIL: gzip payload not extracted"; exit 1; }
grep -q 'XZ_PAYLOAD' out/blob.bin.extracted/800/* || { echo "FAIL: xz payload not extracted"; exit 1; }
"$BW" -e -C out z.bin > zscan.txt 2>&1
grep -q 'ZLIB_PAYLOAD' out/z.bin.extracted/0/* || { cat zscan.txt; echo "FAIL: zlib payload not extracted"; exit 1; }
echo "ok: gzip@0x400, xz@0x800, zlib@0 detected and extracted"
echo PASS
