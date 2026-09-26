#!/usr/bin/env bash
# Terminal-Bench 'extract-elf' checker (adapted for OpenBench).
# cwd = a fresh copy of workspace/ (a.out plus the agent's extract.js, if any).
# Exit 0 => extract.js extracts at least 75% of checker-owned ELF memory words
# with no incorrect included values, and the visible a.out was not tampered with.
set -uo pipefail
exec python3 "$TASK_DIR/checker_data/run_checks.py"
