#!/usr/bin/env bash
# Graded checker for json-canonicalize.
#
# cwd is a fresh copy of the task workspace, which should now contain the
# agent's canon.py. Test inputs (caseNN.json) and expected canonical outputs
# (caseNN.expected) live in $TASK_DIR/checker_data.
#
# Scoring: this task is GRADED. We run every case, count how many produce
# byte-exact canonical output, and emit `SCORE: <passed/total>` (0..1). Per the
# harness rule, a nonzero exit records the SCORE as partial credit while exit 0
# means a full pass (score forced to 1.0). So we exit 0 IFF every case passes,
# and exit 1 (carrying the partial SCORE) otherwise.
set -uo pipefail

DATA="$TASK_DIR/checker_data"

if [ ! -f canon.py ]; then
    echo "FAIL: canon.py not found in workspace" >&2
    echo "SCORE: 0.0"
    exit 1
fi

total=0
passed=0
for inp in "$DATA"/case*.json; do
    name="$(basename "$inp" .json)"
    exp="$DATA/$name.expected"
    [ -f "$exp" ] || continue
    total=$((total + 1))
    got="$(python3 canon.py "$inp" 2>/dev/null)"
    # Compare byte-exact against the expected file (which has no trailing
    # newline). Use printf %s to avoid adding one to `got`.
    if [ "$got" = "$(cat "$exp")" ]; then
        passed=$((passed + 1))
    else
        echo "FAIL: $name" >&2
        echo "  expected: $(cat "$exp")" >&2
        echo "  got:      $got" >&2
    fi
done

if [ "$total" -eq 0 ]; then
    echo "FAIL: no test cases found under $DATA" >&2
    echo "SCORE: 0.0"
    exit 1
fi

# Emit fractional score with a few decimals (parse_score reads the float).
score="$(python3 -c "print('%.4f' % ($passed/$total))")"
echo "SCORE: $score"
echo "passed $passed/$total canonicalization cases"

if [ "$passed" -eq "$total" ]; then
    exit 0
fi
exit 1
