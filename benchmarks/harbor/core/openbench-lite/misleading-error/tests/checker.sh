#!/usr/bin/env bash
# Binary check: `python3 main.py` must exit 0 and print the expected line.
set -uo pipefail

EXPECTED="Total: 5.0"

got="$(python3 main.py 2>/dev/null)"
status=$?

if [ "$status" -ne 0 ]; then
    echo "FAIL: python3 main.py exited with status $status" >&2
    exit 1
fi

if [ "$got" != "$EXPECTED" ]; then
    echo "FAIL: unexpected output" >&2
    echo "--- expected ---" >&2
    printf '%s\n' "$EXPECTED" >&2
    echo "--- got ---" >&2
    printf '%s\n' "$got" >&2
    exit 1
fi

echo "main.py ran and printed the expected line."
