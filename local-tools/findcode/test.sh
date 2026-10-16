#!/usr/bin/env bash
# Smoke test for findcode (decompals/findcode): builds nothing, only runs bin/findcode.
# Checks on a tiny self-made N64-shaped file: big-endian header is detected and the
# region scan prints a count; a file without the N64 header must be rejected.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
bin="$here/bin/findcode"

if [[ ! -x "$bin" ]]; then
  echo "FAIL: no built binary at $bin. The upstream build stops on fmt consteval errors with clang 21 (see README.md)."
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# 4 KiB file: first word is the N64 big-endian header 0x80371240, rest is zero filler.
python3 -I -c '
import sys, struct
data = struct.pack(">I", 0x80371240) + bytes(4096 - 4)
open(sys.argv[1], "wb").write(data)
' "$tmp/tiny.z64"

out="$("$bin" "$tmp/tiny.z64")"
echo "$out"
grep -q "Detected big endian rom" <<<"$out" || { echo "FAIL: header not detected"; exit 1; }
grep -q "Found [0-9]* code regions" <<<"$out" || { echo "FAIL: no region count printed"; exit 1; }

# A non-N64 file must be rejected with a non-zero exit.
python3 -I -c '
import sys
open(sys.argv[1], "wb").write(b"NOTANN64" * 16)
' "$tmp/bad.bin"
if "$bin" "$tmp/bad.bin" >/dev/null 2>&1; then
  echo "FAIL: non-N64 file was accepted"
  exit 1
fi

echo "PASS: findcode detected the header, printed a region count, and rejected a non-N64 file"
