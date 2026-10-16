#!/usr/bin/env bash
# Smoke test: build a tiny PS1 disc image with mkpsxiso from a self-made file,
# then extract it again with dumpsxiso and compare the file byte-for-byte.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cd "$T"
mkdir src
printf 'decomp.wtf mkpsxiso smoke test\n' > src/HELLO.TXT
head -c 5000 /dev/zero | tr '\0' 'A' > src/DATA.BIN
cat > disc.xml <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<iso_project image_name="test.bin" cue_sheet="test.cue">
  <track type="data">
    <identifiers system="PLAYSTATION" application="PLAYSTATION" volume="SMOKE"/>
    <directory_tree>
      <file name="HELLO.TXT" source="src/HELLO.TXT"/>
      <dir name="SUB">
        <file name="DATA.BIN" source="src/DATA.BIN"/>
      </dir>
    </directory_tree>
  </track>
</iso_project>
XML
"$HERE/bin/mkpsxiso" -q -y disc.xml
[ -s test.bin ] || { echo "FAIL: no image"; exit 1; }
echo "image size: $(wc -c < test.bin) bytes (sectors: $(( $(wc -c < test.bin) / 2352 )))"
"$HERE/bin/dumpsxiso" -q -x out -s out.xml test.bin
cmp src/HELLO.TXT out/HELLO.TXT
cmp src/DATA.BIN out/SUB/DATA.BIN
grep -q 'HELLO.TXT' out.xml
echo PASS
