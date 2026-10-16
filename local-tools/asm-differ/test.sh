#!/usr/bin/env bash
# Smoke test: assemble a "target" and a slightly different "mine" MIPS object, run asm-differ
# in object mode (-o) non-interactively, and check it reports the mismatching instruction
# (json + plain output) and reports no diff when the objects are identical.
# Needs MIPS binutils: $MIPS_AS (objdump next to it), or the decomp.wtf binutils-mips cache,
# or mips-linux-gnu-as/objdump on PATH.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CACHE="$HOME/Library/Application Support/decompnet/toolchains/binutils-mips"
AS="${MIPS_AS:-}"
[ -z "$AS" ] && [ -x "$CACHE/mips-ps2-decompals-as" ] && AS="$CACHE/mips-ps2-decompals-as"
[ -z "$AS" ] && AS="$(command -v mips-linux-gnu-as || true)"
[ -n "$AS" ] || { echo "FAIL: no MIPS assembler found (set MIPS_AS)"; exit 1; }
OBJDUMP="${AS%as}objdump"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cd "$T"
mkdir -p expected
cat > diff_settings.py <<PY
def apply(config, args):
    config["arch"] = "mips"
    config["objdump_executable"] = r"""$OBJDUMP"""
    config["source_directories"] = ["."]
PY
asmfn() { printf '.set noreorder\n.text\n.globl func\n.type func, @function\nfunc:\n  addiu $v0, $a0, %s\n  sll $v0, $v0, 2\n  jr $ra\n  nop\n.size func, .-func\n' "$1"; }
asmfn 5 > target.s; asmfn 6 > mine.s
"$AS" -march=vr4300 -32 -EB target.s -o expected/func.o
"$AS" -march=vr4300 -32 -EB mine.s -o func.o
run() { "$HERE/.venv/bin/asm-differ" -o -f func.o -F expected/func.o --no-pager "$@" func; }
json=$(run --format json)
python3 -I -c '
import json,sys
d=json.loads(sys.argv[1])
assert d["current_score"] > 0, d["current_score"]
assert "immediate" in json.dumps(d["rows"][0]["current"]), "first row should flag the immediate"
print("json: score", d["current_score"], "of max", d["max_score"], "- first row flagged as immediate mismatch")
' "$json"
plain=$(run --format plain); echo "$plain"
grep -q "addiu *v0,a0,5" <<<"$plain"
grep -q "addiu *v0,a0,6" <<<"$plain"
# identical objects -> no differing lines
cp expected/func.o func.o
same=$(run --format json)
python3 -I -c '
import json,sys
d=json.loads(sys.argv[1])
assert d["current_score"] == 0, d["current_score"]
print("identical objects: score 0")
' "$same"
echo PASS
