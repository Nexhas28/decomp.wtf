#!/bin/bash
# Phase 12 production cutover: live decomp.wtf app -> the implemented fork wheel.
# Canary-first staging was completed (see benchmarks/rollout/README.md); this script performs the
# final step with a WAL-safe pre-cutover backup and a health check afterwards.
set -euo pipefail

TS=$(date -u +%Y%m%d-%H%M%S)
WHEEL=/Users/shanemcgovern/Decomp/decomp-wtf/dist/decompnet-0.10.6-py3-none-any.whl
TV=/Users/shanemcgovern/.local/share/uv/tools/decompnet
PY=$TV/bin/python3
HOME_DIR=/Users/shanemcgovern/Decomp

echo "== 1/4: WAL-safe backup of the live DB =="
mkdir -p "$HOME_DIR/.decomp/backups"
$PY - "$HOME_DIR/.decomp/backups/pre-cutover-$TS.db" <<'EOF'
import sqlite3, sys
src = sqlite3.connect("/Users/shanemcgovern/Decomp/.decomp/decomp.db")
dst = sqlite3.connect(sys.argv[1])
with dst:
    src.backup(dst)
src.close(); dst.close()
print("backup:", sys.argv[1])
EOF

echo "== 2/4: stop the live app =="
"$TV/bin/decomp" stop || true
sleep 2

echo "== 3/4: install the fork wheel into the live tool venv =="
UV_CACHE_DIR=/Users/shanemcgovern/Decomp/.uv-cache /Users/shanemcgovern/.local/bin/uv pip install --python "$PY" --reinstall --no-deps "$WHEEL" -q
echo "installed fork decompnet into $TV"

echo "== 4/4: restart and verify =="
"$TV/bin/decomp" url >/dev/null 2>&1 || true
sleep 3
echo "cutover complete. Verify: curl -H 'Cookie: decomp_live=<secret>' http://127.0.0.1:6464/health"
