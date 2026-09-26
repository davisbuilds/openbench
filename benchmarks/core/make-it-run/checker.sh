#!/usr/bin/env bash
# Runs with cwd set to a fresh copy of the task workspace.
# Exit 0 => `python3 main.py` runs and prints the expected line.
set -uo pipefail

EXPECTED="Hello, world!"

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
