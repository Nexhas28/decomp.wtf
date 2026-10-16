#!/usr/bin/env bash
# Smoke test for pret GBA tools: gbagfx (4bpp<->png, LZ77), preproc, scaninc, gbafix.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; B="$HERE/bin"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT; cd "$W"

# gbagfx: 2 tiles of 4bpp data -> PNG -> 4bpp must round-trip byte-exact
python3 -c "import sys; sys.stdout.buffer.write(bytes(((i*7)&0xFF) for i in range(64)))" > t.4bpp
"$B/gbagfx" t.4bpp t.png -mwidth 1
"$B/gbagfx" t.png t2.4bpp
cmp t.4bpp t2.4bpp
# gbagfx: BIOS-compatible LZ77 compress + decompress round-trip, header 0x10 + size 0x40
"$B/gbagfx" t.4bpp t.4bpp.lz
"$B/gbagfx" t.4bpp.lz t3.4bpp
cmp t.4bpp t3.4bpp
[ "$(xxd -p -l 4 t.4bpp.lz)" = "10400000" ]

# preproc: charmap-encoded .string
printf "'A' = BB\n'B' = BC\n'\$' = FF\n" > charmap.txt
printf '\t.string "AB$"\n' > t.s
"$B/preproc" t.s charmap.txt | grep -q '\.byte 0xBB, 0xBC, 0xFF'

# scaninc: transitive include scan
printf '#include "foo.h"\nint x;\n' > a.c; printf '#include "bar.h"\n' > foo.h; : > bar.h
out="$("$B/scaninc" a.c)"; grep -q foo.h <<<"$out"; grep -q bar.h <<<"$out"

# gbafix: patch header of a self-made 0xD8-byte ROM image, verify title + complement checksum
python3 - <<'PY'
rom = bytearray(0xD8); rom[0:4] = bytes([0x2e,0,0,0xea]); rom[0xB2] = 0x96
open("r.gba","wb").write(rom)
PY
"$B/gbafix" r.gba -tTESTROM -cABCD -mZZ -r1 >/dev/null
python3 - <<'PY'
r = open("r.gba","rb").read()
assert r[0xA0:0xA7] == b"TESTROM" and r[0xAC:0xB0] == b"ABCD" and r[0xBC] == 1
chk = (-(sum(r[0xA0:0xBD]) + 0x19)) & 0xFF
assert r[0xBD] == chk, (r[0xBD], chk)
PY
echo "PASS pret-gba-tools"
