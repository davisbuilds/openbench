#!/usr/bin/env bash
# Scores solution.py against the imported canonical cases. Prints a SCORE line
# and exits 0 only when every case passes. Runs with cwd = the agent's
# workspace copy; the scorer and cases live under $TASK_DIR/checker_data.
set -uo pipefail

exec python3 "$TASK_DIR/checker_data/run_score.py"
