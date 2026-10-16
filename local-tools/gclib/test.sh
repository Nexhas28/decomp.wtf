#!/usr/bin/env bash
# Smoke test for gclib: Yaz0 compress/decompress round trip, and building a RARC archive
# from scratch, saving it, re-reading it and extracting the file. Inputs are made up here.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cat > "$tmp/t.py" <<'PY'
import os, sys
from io import BytesIO
from gclib.yaz0_yay0 import Yaz0
from gclib.rarc import RARC

out = sys.argv[1]
payload = (b"decomp.wtf gclib smoke test " * 40) + bytes(range(256))

# 1. Yaz0 round trip
comp = Yaz0.compress(BytesIO(payload))
cb = comp.getvalue()
assert cb[:4] == b"Yaz0", cb[:4]
assert len(cb) < len(payload), (len(cb), len(payload))
dec = Yaz0.decompress(BytesIO(cb)).getvalue()
assert dec == payload, "Yaz0 round trip mismatch"
print("yaz0 ok: %d -> %d -> %d bytes" % (len(payload), len(cb), len(dec)))

# 2. Build a RARC from scratch, save, re-read, extract
r = RARC()
r.add_root_directory()
r.add_new_file("hello.bin", BytesIO(payload), r.nodes[0])
r.save_changes()
raw = r.data.getvalue()
assert raw[:4] == b"RARC", raw[:4]
path = os.path.join(out, "t.arc")
with open(path, "wb") as f:
    f.write(raw)
r2 = RARC(path)
names = [e.name for e in r2.file_entries if not e.is_dir]
assert names == ["hello.bin"], names
r2.extract_all_files_to_disk(os.path.join(out, "x"))
with open(os.path.join(out, "x", "hello.bin"), "rb") as f:
    assert f.read() == payload, "RARC extracted content mismatch"
print("rarc ok: %d byte archive, files=%s" % (len(raw), names))
PY

"$here/.venv/bin/python" -I "$tmp/t.py" "$tmp"
echo "gclib smoke test PASS"
