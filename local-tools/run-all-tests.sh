#!/usr/bin/env bash
# Run every local-tools/<tool>/test.sh and print a pass/fail summary.
cd "$(dirname "$0")"
pass=0; fail=0; failed=()
for t in */test.sh; do
  tool=${t%/test.sh}
  if bash "$t" >/tmp/local-tools-test-$tool.log 2>&1; then
    echo "PASS $tool"; pass=$((pass+1))
  else
    echo "FAIL $tool (log: /tmp/local-tools-test-$tool.log)"; fail=$((fail+1)); failed+=("$tool")
  fi
done
echo "---"; echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
