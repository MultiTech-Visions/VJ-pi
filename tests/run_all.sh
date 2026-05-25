#!/bin/bash
# Run every test in tests/ in order, capture the combined log to
# tests/output/run.log. Each test is its own process so a crash in
# one doesn't take out the rest.
#
# Usage:  ./tests/run_all.sh
#
# After it finishes, paste tests/output/run.log AND look at the
# tests/output/*.png files — the PNGs show what each test actually
# rendered (or didn't), the log says PASS/FAIL with diagnostics.

set -uo pipefail

# Resolve project root from this script's location so the runner
# works whether you double-click it or `./tests/run_all.sh` it.
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

LOG="$HERE/output/run.log"
mkdir -p "$HERE/output"
: >"$LOG"

if [ -x "./venv/bin/python" ]; then
  PY="./venv/bin/python"
else
  PY="python3"
fi

echo "============================================================" | tee -a "$LOG"
echo "  pi-paint VJ — diagnostic test run" | tee -a "$LOG"
echo "  $(date)" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo "" | tee -a "$LOG"

PASS=0
FAIL=0
for test in "$HERE"/test_*.py; do
  name="$(basename "$test")"
  echo "" | tee -a "$LOG"
  echo "--- $name ---" | tee -a "$LOG"
  if "$PY" "$test" 2>&1 | tee -a "$LOG"; then
    PASS=$((PASS + 1))
    echo "  → PASS" | tee -a "$LOG"
  else
    FAIL=$((FAIL + 1))
    echo "  → FAIL" | tee -a "$LOG"
  fi
done

echo "" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"
echo "  Summary: $PASS passed, $FAIL failed" | tee -a "$LOG"
echo "  Log:  $LOG" | tee -a "$LOG"
echo "  PNGs: $HERE/output/*.png" | tee -a "$LOG"
echo "============================================================" | tee -a "$LOG"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
