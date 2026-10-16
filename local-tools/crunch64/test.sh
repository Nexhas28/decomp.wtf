#!/usr/bin/env bash
# Smoke test: round-trip a self-made file through every N64 compression format
# (Yay0, Yaz0, MIO0, gzip) and check the header magic + exact decompressed bytes.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
C="$HERE/bin/crunch64"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cd "$T"
# repetitive data so the LZ formats actually compress
for i in $(seq 1 200); do printf 'decomp.wtf crunch64 line %03d ABCABCABC\n' $((i % 7)); done > in.bin
for fmt in yay0 yaz0 mio0; do
  "$C" compress "$fmt" in.bin "c.$fmt"
  "$C" decompress "$fmt" "c.$fmt" "d.$fmt"
  cmp in.bin "d.$fmt"
  echo "$fmt: $(wc -c < in.bin | tr -d ' ') -> $(wc -c < "c.$fmt" | tr -d ' ') bytes, round-trip OK"
done
# gzip mode emits raw deflate (as N64 games store it); the CLI only compresses it,
# so verify with Python's zlib inflater.
"$C" compress gzip in.bin c.gzip
python3 -I -c 'import sys,zlib; d=zlib.decompress(open(sys.argv[1],"rb").read(),-15); sys.exit(d!=open(sys.argv[2],"rb").read())' c.gzip in.bin
echo "gzip(raw deflate): $(wc -c < c.gzip | tr -d ' ') bytes, inflates back OK"
[ "$(head -c4 c.yay0)" = "Yay0" ] || { echo "FAIL Yay0 magic"; exit 1; }
[ "$(head -c4 c.yaz0)" = "Yaz0" ] || { echo "FAIL Yaz0 magic"; exit 1; }
[ "$(head -c4 c.mio0)" = "MIO0" ] || { echo "FAIL MIO0 magic"; exit 1; }
[ "$(wc -c < c.yaz0)" -lt "$(wc -c < in.bin)" ] || { echo "FAIL: yaz0 did not compress"; exit 1; }
echo PASS
