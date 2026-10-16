#!/usr/bin/env bash
# Smoke test: create a .nds ROM from scratch with ndspy (ARM9 code + NitroFS folder/file),
# save and reload it, then round-trip LZ10 compression and a NARC archive.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
py="$here/.venv/bin/python"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
"$py" -I - "$tmp" <<'PY'
import sys, os
import ndspy, ndspy.rom, ndspy.fnt, ndspy.lz10, ndspy.narc, ndspy.code
tmp = sys.argv[1]
arm9 = bytes.fromhex("2a00a0e3feffffea")        # mov r0,#0x2A ; b .
rom = ndspy.rom.NintendoDSRom()
rom.name = b"DECOMPWTF"
rom.idCode = b"DWTF"
rom.arm9 = arm9
rom.arm7 = bytes.fromhex("0710a0e3feffffea")
root = ndspy.fnt.Folder(firstID=0)
data = ndspy.fnt.Folder(files=["hello.txt"], firstID=0)
root.folders.append(("data", data))
rom.filenames = root
rom.files = [b"hello-decomp\n"]
path = os.path.join(tmp, "t.nds")
rom.saveToFile(path)

r2 = ndspy.rom.NintendoDSRom.fromFile(path)
assert r2.name.rstrip(b"\0") == b"DECOMPWTF", r2.name
assert r2.idCode == b"DWTF"
assert r2.arm9[:8] == arm9
assert r2.getFileByName("data/hello.txt") == b"hello-decomp\n"
r2.setFileByName("data/hello.txt", b"patched\n")
r2.saveToFile(path)
assert ndspy.rom.NintendoDSRom.fromFile(path).getFileByName("data/hello.txt") == b"patched\n"

blob = b"decomp.wtf " * 64
comp = ndspy.lz10.compress(blob)
assert len(comp) < len(blob) and ndspy.lz10.decompress(comp) == blob

n = ndspy.narc.NARC()
n.filenames = ndspy.fnt.Folder(files=["a.bin", "b.bin"])
n.files = [b"\x01\x02\x03\x04", b"BBBB"]
n2 = ndspy.narc.NARC(n.save())
assert n2.getFileByName("b.bin") == b"BBBB"
print("checks ok")
PY
echo "ndspy: PASS"
