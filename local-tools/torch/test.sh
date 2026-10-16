#!/usr/bin/env bash
# Smoke test for Torch (HarbourMasters N64 asset processor).
# Uses only inputs created here: a text folder for `pack` and a fake
# 64 KB N64-shaped blob for `binary` mode. No ROMs or game data.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TORCH="$HERE/bin/torch"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/torch-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"  # Torch writes debug/ and output files into the cwd

fail() { echo "FAIL: $*" >&2; exit 1; }

[ -x "$TORCH" ] || fail "binary not found at $TORCH"

is_mpq() { [ "$(head -c 3 "$1")" = "MPQ" ] || fail "$1 is not an MPQ archive"; }

# 1. pack: build an OTR from a tiny folder of text files
mkdir -p "$WORK/pack/in/sub"
printf 'hello torch\n' > "$WORK/pack/in/a.txt"
printf 'second file\n' > "$WORK/pack/in/sub/b.txt"
"$TORCH" pack "$WORK/pack/in" "$WORK/pack/out.otr" > "$WORK/pack/log.txt" 2>&1 \
  || fail "torch pack exited non-zero"
[ -s "$WORK/pack/out.otr" ] || fail "pack produced no output file"
is_mpq "$WORK/pack/out.otr"
grep -q "Added sub/b.txt" "$WORK/pack/log.txt" || fail "pack did not log sub/b.txt"
echo "pack: ok ($(wc -c < "$WORK/pack/out.otr") bytes, MPQ header, 2 files added)"

# 2. binary: run the ROM pipeline on a fake N64-shaped file with a matching config.
# Torch keys config.yml by the hash it computes for the ROM, so first run with a
# placeholder config to learn the hash for this fake file, then write the real config.
mkdir -p "$WORK/rom/assets"
cd "$WORK/rom"
{ printf '\x80\x37\x12\x40'; head -c 65532 /dev/zero; } > fake.z64
printf 'PLACEHOLDER:\n  config:\n    segments: []\n' > config.yml
"$TORCH" binary fake.z64 > probe.log 2>&1 || true
ROM_HASH="$(sed -n 's/.*No config found for \([0-9a-f]\{40\}\).*/\1/p' probe.log | head -n 1)"
[ -n "$ROM_HASH" ] || fail "could not read ROM hash from torch output (see $WORK/rom/probe.log)"
cat > config.yml <<YAML
$ROM_HASH:
  name: Fake
  path: assets/
  config:
    gbi: F3D
    sort: OFFSET
    output:
      binary: fake.otr
      headers: include
      code: code
    segments:
      - 0x12345678
YAML
"$TORCH" binary fake.z64 > run.log 2>&1 || fail "torch binary exited non-zero (see $WORK/rom/run.log)"
grep -q "No config found" run.log && fail "config lookup by ROM hash failed"
[ -s fake.otr ] || fail "binary mode produced no fake.otr"
is_mpq fake.otr
echo "binary: ok (ROM hash $ROM_HASH matched, fake.otr written with MPQ header)"

echo "PASS"
