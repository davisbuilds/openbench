#!/usr/bin/env bash
# Terminal-Bench 'count-call-stack' checker (adapted for OpenBench).
# cwd = a fresh copy of workspace/ (with the agent's output.txt, if any).
# Exit 0 => output.txt matches the upstream expected output (line-by-line, rstripped).
set -uo pipefail
exec python3 "$TASK_DIR/checker_data/run_checks.py"
