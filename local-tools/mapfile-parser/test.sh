#!/usr/bin/env bash
# Smoke test for mapfile_parser (ethteck/Decompollaborate, pinned to 2.9.0).
# Builds a tiny GNU ld style map file, then checks:
#   1. the Python API (MapFile.readMapFile) parses the sections and symbols
#   2. the bundled CLI (python -m mapfile_parser jsonify) writes JSON with the expected symbol
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/.venv/bin/python"

if [ ! -x "$PY" ]; then
    echo "FAIL: venv missing at $PY (run: uv venv .venv && uv pip install mapfile_parser)" >&2
    exit 1
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/mapfile-parser-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

MAP="$TMP/sample.map"
cat > "$MAP" <<'EOF'
Archive member included to satisfy reference by file (symbol)

Linker script and memory map

.text           0x0000000080000000       0x20
 *(.text)
 .text          0x0000000080000000       0x20 build/main.o
                0x0000000080000000                func_80000000
                0x0000000080000010                func_80000010

.data           0x0000000080000020        0x8
 .data          0x0000000080000020        0x8 build/main.o
                0x0000000080000020                data_80000020
EOF

echo "== 1. Python API parse =="
"$PY" -I - "$MAP" <<'EOF'
import json, sys
from pathlib import Path
from mapfile_parser import MapFile

mf = MapFile()
mf.readMapFile(Path(sys.argv[1]))
dumped = json.dumps(mf.toJson(humanReadable=True))
for needle in ("func_80000000", "func_80000010", "data_80000020"):
    if needle not in dumped:
        print(f"FAIL: symbol {needle} missing from API output", file=sys.stderr)
        sys.exit(1)
print("API: found func_80000000, func_80000010, data_80000020")
EOF

echo "== 2. CLI jsonify =="
OUT="$TMP/out.json"
"$PY" -I -m mapfile_parser jsonify "$MAP" -o "$OUT"
if [ ! -s "$OUT" ]; then
    echo "FAIL: CLI did not write $OUT" >&2
    exit 1
fi
"$PY" -I -c '
import json, sys
data = json.load(open(sys.argv[1]))
text = json.dumps(data)
for needle in ("func_80000010", "data_80000020"):
    if needle not in text:
        print(f"FAIL: symbol {needle} missing from CLI JSON", file=sys.stderr)
        sys.exit(1)
print("CLI: jsonify output contains func_80000010 and data_80000020")
' "$OUT"

echo "PASS: mapfile_parser API and CLI smoke test"
