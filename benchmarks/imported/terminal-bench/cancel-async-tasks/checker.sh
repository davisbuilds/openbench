#!/usr/bin/env bash
# Terminal-Bench 'cancel-async-tasks' checker (adapted for OpenBench).
# cwd = a fresh copy of workspace/ (with the agent's run.py, if any).
# Exit 0 => all concurrency/cancellation scenarios pass.
set -uo pipefail

# The upstream harness (tests/test.py) imports `run_tasks` from run.py in cwd.
cp "$TASK_DIR/checker_data/test.py" ./test.py

exec python3 "$TASK_DIR/checker_data/run_checks.py"
