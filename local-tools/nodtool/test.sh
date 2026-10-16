#!/usr/bin/env bash
# Smoke test: hand-build a tiny GameCube GCM disc image, then inspect, extract and
# round-trip it through RVZ compression with nodtool.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
nod="$here/bin/nodtool"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/mkgcm.py" <<'PY'
import struct, sys
img = bytearray(0x20000)
img[0:6] = b"DWTF01"                                   # game id
struct.pack_into(">I", img, 0x1C, 0xC2339F3D)          # GameCube magic
name = b"DECOMP WTF TEST DISC"; img[0x20:0x20+len(name)] = name
APP = 0x2440                                           # apploader: header + 'blr'
img[APP:APP+10] = b"2026/10/16"
struct.pack_into(">III", img, APP+0x10, 0x81200000, 4, 0)
img[APP+0x20:APP+0x24] = bytes.fromhex("4e800020")
DOL = 0x3000                                           # DOL: one text section 'li r3,42; blr'
text = bytes.fromhex("3860002a4e800020")
hdr = bytearray(0x100)
struct.pack_into(">I", hdr, 0x00, 0x100)
struct.pack_into(">I", hdr, 0x48, 0x80003100)
struct.pack_into(">I", hdr, 0x90, len(text))
struct.pack_into(">II", hdr, 0xD8, 0x80004000, 0)
struct.pack_into(">I", hdr, 0xE0, 0x80003100)
img[DOL:DOL+0x108] = bytes(hdr) + text
FILE = 0x8000; data = b"hello-decomp\n"
img[FILE:FILE+len(data)] = data
FST = 0x4000                                           # FST: root / data/ / hello.txt
fst = struct.pack(">III", 0x01000000, 0, 3) + struct.pack(">III", 0x01000000, 0, 3) \
    + struct.pack(">III", 5, FILE, len(data)) + b"data\0hello.txt\0"
img[FST:FST+len(fst)] = fst
struct.pack_into(">IIII", img, 0x420, DOL, FST, len(fst), len(fst))
open(sys.argv[1], "wb").write(img)
PY
python3 -I "$tmp/mkgcm.py" "$tmp/t.iso"
"$nod" info "$tmp/t.iso" > "$tmp/info.txt" 2>&1
grep -q 'Game ID: DWTF01' "$tmp/info.txt"
grep -q 'DECOMP WTF TEST DISC' "$tmp/info.txt"
"$nod" extract -q "$tmp/t.iso" "$tmp/x"
[ "$(cat "$tmp/x/files/data/hello.txt")" = "hello-decomp" ]
# main.dol extracted intact: text bytes at 0x100 are 'li r3,42; blr'
[ "$(xxd -p -s 0x100 -l 8 "$tmp/x/sys/main.dol")" = "3860002a4e800020" ]
"$nod" convert "$tmp/t.iso" "$tmp/t.rvz" >/dev/null 2>&1
"$nod" info "$tmp/t.rvz" > "$tmp/rvz.txt" 2>&1; grep -q "Format: RVZ" "$tmp/rvz.txt"
"$nod" convert "$tmp/t.rvz" "$tmp/back.iso" >/dev/null 2>&1
cmp "$tmp/t.iso" "$tmp/back.iso"
echo "nodtool: PASS"
