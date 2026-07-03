#!/usr/bin/env bash
# Runs with cwd set to a fresh copy of the task workspace (which should now
# contain the agent's wordcount.py). Test inputs and expected outputs live in
# the task's own checker_data/ directory, referenced via $TASK_DIR.
# Exit 0 => all cases pass, nonzero => failed.
set -euo pipefail

DATA="$TASK_DIR/checker_data"

if [ ! -f wordcount.py ]; then
    echo "FAIL: wordcount.py not found in workspace" >&2
    exit 1
fi

# Each entry: "<case name> <N>"
cases="case1 3
case2 2
case3 5"

fail=0
while read -r name n; do
    [ -z "$name" ] && continue
    got="$(python3 wordcount.py "$DATA/$name.txt" "$n")"
    want="$(cat "$DATA/$name.expected")"
    if [ "$got" != "$want" ]; then
        echo "FAIL: $name (N=$n)" >&2
        echo "--- expected ---" >&2
        printf '%s\n' "$want" >&2
        echo "--- got ---" >&2
        printf '%s\n' "$got" >&2
        fail=1
    fi
done <<EOF
$cases
EOF

if [ "$fail" -ne 0 ]; then
    exit 1
fi
echo "All wordcount cases passed."
