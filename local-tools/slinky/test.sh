#!/usr/bin/env bash
# Smoke test for slinky-cli (linker script generator, decompals/slinky 0.6.0).
# Writes a tiny segment description, generates a linker script from it and
# checks that the script contains the expected sections, object paths and
# the fixed VRAM address. Exits 0 on pass.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HERE/bin/slinky-cli"

if [[ ! -x "$BIN" ]]; then
    echo "FAIL: $BIN not found or not executable" >&2
    exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/tiny.yaml" <<'EOF'
settings:
  base_path: build
  subalign: 32

segments:
  - name: header
    files:
      - { path: asm/data/rom_header.o }

  - name: entry
    fixed_vram: 0x80000400
    files:
      - { path: asm/entry.o }
EOF

# 1) Generate to a file.
"$BIN" "$TMP/tiny.yaml" -o "$TMP/out.ld" --omit-version-comment

check() {
    local needle="$1"
    if ! grep -qF -- "$needle" "$TMP/out.ld"; then
        echo "FAIL: generated script is missing: $needle" >&2
        echo "--- script tail ---" >&2
        tail -n 20 "$TMP/out.ld" >&2
        exit 1
    fi
}

check "SECTIONS"
check ".header : AT(header_ROM_START) SUBALIGN(32)"
check "build/asm/data/rom_header.o(.text*);"
check "build/asm/entry.o(.text*);"
check "0x80000400"

# 2) Generate to stdout and confirm the same content comes out.
"$BIN" "$TMP/tiny.yaml" --omit-version-comment > "$TMP/stdout.ld"
if ! grep -qF -- "build/asm/entry.o(.text*);" "$TMP/stdout.ld"; then
    echo "FAIL: stdout output is missing the entry object mapping" >&2
    exit 1
fi

# 3) A missing input file must make the tool fail (non-zero exit).
if "$BIN" "$TMP/does-not-exist.yaml" > /dev/null 2>&1; then
    echo "FAIL: slinky-cli accepted a missing input file" >&2
    exit 1
fi

echo "PASS: slinky-cli generated a linker script with expected sections, object paths and fixed VRAM"
exit 0
