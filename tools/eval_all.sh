#!/bin/sh
# Run every labeled case file N times (default 3) and report each miss and the totals.
cd "$(dirname "$0")/.." || exit 1
n=${1:-3}
for i in $(seq "$n"); do
  for f in tests/data/scope_cases*.json; do python3 tools/eval_questions.py "$f"; done | grep -E "^XX|correct at" | awk -v run="$i" '/^XX/ {print "run " run ": " $0} /correct at/ {split($1, a, "/"); ok += a[1]; all += a[2]} END {print "run " run ": " ok "/" all " correct"}'
done
