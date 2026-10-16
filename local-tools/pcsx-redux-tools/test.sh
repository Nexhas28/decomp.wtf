#!/usr/bin/env bash
# Smoke test for pcsx-redux-tools (psyq-obj-parser + ps1-packer).
# 1. Hand-builds a tiny PSY-Q LNK (.OBJ) with one .text section holding
#    `addiu v0,zero,42 ; jr ra` and an exported symbol `my_func`, converts it
#    to a MIPS ELF with psyq-obj-parser, and checks the ELF header, machine,
#    symbol name and code bytes.
# 2. Hand-builds a tiny PS-EXE, packs it with ps1-packer, and checks the output
#    is a 2048-byte-aligned PS-EXE (and that -cpe emits a CPE file).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HERE/bin"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

python3 -I - "$T" <<'PY'
import struct, sys, os
t = sys.argv[1]
code = struct.pack('<II', 0x2402002a, 0x03e00008)  # addiu v0,zero,42 ; jr ra
def pstr(s): return bytes([len(s)]) + s.encode()
lnk = b'LNK\x02'
lnk += bytes([46, 7])                                        # PROGRAMTYPE 7
lnk += bytes([16]) + struct.pack('<HHB', 1, 0, 8) + pstr('.text')  # SECTION
lnk += bytes([6]) + struct.pack('<H', 1)                     # SWITCH .text
lnk += bytes([2]) + struct.pack('<H', len(code)) + code      # BYTES
lnk += bytes([12]) + struct.pack('<HHI', 2, 1, 0) + pstr('my_func')  # EXPORTED_SYMBOL
lnk += bytes([0])                                            # END
open(os.path.join(t, 'test.obj'), 'wb').write(lnk)

# Minimal PS-EXE: 2048-byte header + 2048 bytes of code at 0x80010000
body = (code + b'\x00' * 2048)[:2048]
hdr = bytearray(2048)
hdr[0:8] = b'PS-X EXE'
struct.pack_into('<IIIII', hdr, 0x10, 0x80010000, 0, 0x80010000, len(body), 0)
struct.pack_into('<II', hdr, 0x30, 0x801FFFF0, 0)
hdr[0x4C:0x4C+len(b'Sony Computer Entertainment Inc. for North America area')] = b'Sony Computer Entertainment Inc. for North America area'
open(os.path.join(t, 'in.ps-exe'), 'wb').write(bytes(hdr) + body)
PY

echo "== psyq-obj-parser -d"
"$BIN/psyq-obj-parser" "$T/test.obj" -d | tee "$T/dump.txt"
grep -q 'my_func' "$T/dump.txt"
"$BIN/psyq-obj-parser" "$T/test.obj" -o "$T/test.o" >/dev/null

python3 -I - "$T/test.o" <<'PY'
import struct, sys
d = open(sys.argv[1], 'rb').read()
assert d[:4] == b'\x7fELF', 'not an ELF'
assert d[5] == 1, 'not little-endian'
mach = struct.unpack_from('<H', d, 18)[0]
assert mach == 8, f'e_machine {mach} != EM_MIPS'
assert struct.pack('<II', 0x2402002a, 0x03e00008) in d, 'code bytes missing'
assert b'my_func\x00' in d, 'symbol missing'
print(f'ELF ok: EM_MIPS little-endian, {len(d)} bytes, code + my_func present')
PY

echo "== ps1-packer"
"$BIN/ps1-packer" "$T/in.ps-exe" -o "$T/out.ps-exe"
python3 -I - "$T/out.ps-exe" <<'PY'
import struct, sys
d = open(sys.argv[1], 'rb').read()
assert d[:8] == b'PS-X EXE', 'packed output lacks PS-X EXE magic'
assert len(d) % 2048 == 0, 'packed output not 2048-aligned'
pc0, gp, taddr, tsize = struct.unpack_from('<IIII', d, 0x10)
assert tsize == len(d) - 2048, 't_size mismatch'
print(f'packed PS-EXE ok: {len(d)} bytes, pc0=0x{pc0:08x} t_addr=0x{taddr:08x}')
PY
"$BIN/ps1-packer" "$T/in.ps-exe" -cpe -o "$T/out.cpe"
head -c 3 "$T/out.cpe" | grep -q 'CPE'
echo "CPE output ok"
echo "PASS"
