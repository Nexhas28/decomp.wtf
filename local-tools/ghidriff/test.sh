#!/usr/bin/env bash
# Smoke test: build two tiny x86-64 ELF programs (v1/v2) where only foo() changes, diff them with ghidriff
# (Ghidra 12.1.4 via pyghidra, headless; install untouched; project/output dirs live in a mktemp dir),
# and check the markdown/json diff reports foo as modified while bar is not.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LLD="${LLD_BIN:-/opt/homebrew/opt/lld@21/bin/ld.lld}"
LLVMBIN="${LLVM_BIN:-/opt/homebrew/opt/llvm@21/bin}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT; cd "$W"
mk() { # $1=name $2=foo body
cat > "$1.c" <<C
int counter = 5;
__attribute__((noinline)) int bar(int x) { return x * 3 + 1; }
__attribute__((noinline)) int foo(int x) { $2 }
void _start(void) { for (;;) { foo(2); bar(counter); } }
C
"$LLVMBIN/clang" -target x86_64-unknown-linux-gnu -O1 -fno-pic -fno-pie -ffreestanding -fno-stack-protector \
  -fno-asynchronous-unwind-tables -mno-red-zone -c "$1.c" -o "$1.o"
"$LLD" -static -e _start -o "$1.elf" "$1.o"
}
mk v1 'counter += x; return bar(counter);'
mk v2 'counter += x; counter ^= 0x55; return bar(counter) + 7;'
mkdir -p out
"$HERE/bin/ghidriff" v1.elf v2.elf -o "$W/out" -p "$W/proj" -n t -s "$W/sym" -g "$W/gzfs" --no-symbols --log-path "$W/g.log" > log.txt 2>&1 \
  || { tail -60 log.txt; echo "FAIL ghidriff (run)"; exit 1; }
tail -15 log.txt
JSON="$(find out -name '*.ghidriff.json' | head -1)"; MD="$(find out -name '*.md' | head -1)"
test -s "$JSON" && test -s "$MD" || { tail -40 log.txt; echo "FAIL ghidriff: no report produced"; exit 1; }
"$HERE/.venv/bin/python" -I - "$JSON" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
mod = d["functions"]["modified"]
names = [f.get("old", {}).get("name") or f.get("name") for f in mod] if isinstance(mod, list) else list(mod)
print("modified:", names)
assert any("foo" in str(n) for n in names), names
assert not any(str(n).endswith("bar") for n in names), names
PY
grep -q 'foo' "$MD"
echo "PASS ghidriff"
