#!/usr/bin/env bash
# Smoke test: install the delinker extension into a throwaway Ghidra user-settings dir (user.home is
# redirected; the real Ghidra install and ~/Library/ghidra are untouched), build a tiny x86-64 ELF
# executable, import + analyze it headless, then delink one function with the bundled DelinkProgram.java
# script into a relocatable ELF .o and check the .o has the rebuilt relocations (call -> bar, data -> counter).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
GHIDRA="${GHIDRA_INSTALL_DIR:-/Users/shanemcgovern/Documents/Codex/2026-10-14/i-want/work/ghidra-displaylink/ghidra_12.1.4_PUBLIC}"
export JAVA_HOME="${JAVA_HOME_21:-/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home}"
LLD="${LLD_BIN:-/opt/homebrew/opt/lld@21/bin/ld.lld}"
LLVMBIN="${LLVM_BIN:-/opt/homebrew/opt/llvm@21/bin}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT; cd "$W"
VER="$(sed -n 's/^application.version=//p' "$GHIDRA/Ghidra/application.properties")"
EXT="$W/home/Library/ghidra/ghidra_${VER}_PUBLIC/Extensions"; mkdir -p "$EXT"
unzip -q "$HERE"/ghidra_*_ghidra-delinker-extension.zip -d "$EXT"
export JAVA_TOOL_OPTIONS="-Duser.home=$W/home"

# --- tiny freestanding x86-64 ELF program (self-written) ---
cat > prog.c <<'C'
int counter = 5;
__attribute__((noinline)) int bar(int x) { return x * 3 + 1; }
__attribute__((noinline)) int foo(int x) { counter += x; return bar(counter); }
void _start(void) { for (;;) { foo(2); } }
C
clang -target x86_64-unknown-linux-gnu -O1 -fno-pic -fno-pie -ffreestanding -fno-stack-protector \
  -fno-asynchronous-unwind-tables -mno-red-zone -c prog.c -o prog.o
"$LLD" -static -e _start -o prog.elf prog.o

# --- delink foo() with headless DelinkProgram.java ---
mkdir proj out
"$GHIDRA/support/analyzeHeadless" "$W/proj" p -import prog.elf \
   -scriptPath "$EXT/ghidra-delinker-extension/ghidra_scripts" \
   -postScript DelinkProgram.java /exporter "ELF relocatable object" /include foo /export "$W/out/foo.o" \
   -deleteProject > log.txt 2>&1 || { tail -60 log.txt; echo "FAIL ghidra-delinker (analyzeHeadless)"; exit 1; }

test -s out/foo.o || { tail -60 log.txt; echo "FAIL ghidra-delinker: no foo.o produced"; exit 1; }
H="$("$LLVMBIN/llvm-readelf" -h out/foo.o)"   # captured first: grep -q + pipefail would trip on SIGPIPE
grep -q 'Type:.*REL (Relocatable file)' <<<"$H"
grep -q 'Machine:.*Advanced Micro Devices X86-64' <<<"$H"
R="$("$LLVMBIN/llvm-objdump" -r out/foo.o)"
echo "$R"
grep -q 'R_X86_64_.*bar' <<<"$R"      # tail call foo -> bar became a relocation against bar
grep -q 'R_X86_64_.*counter' <<<"$R"  # access to the global became a relocation against counter
N="$("$LLVMBIN/llvm-nm" out/foo.o)"; echo "$N"
grep -q ' T foo' <<<"$N"
grep -q ' U bar' <<<"$N"
grep -q ' U counter' <<<"$N"
echo "PASS ghidra-delinker"
