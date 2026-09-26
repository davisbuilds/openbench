#!/usr/bin/env bash
# Scores the catalog test suite. Prints a `SCORE: <0.0-1.0>` line (partial
# credit) and exits 0 only when every test passes. Runs with cwd = the agent's
# workspace copy; the scoring harness and its pristine test suite live under
# $TASK_DIR/checker_data.
set -uo pipefail

exec python3 "$TASK_DIR/checker_data/run_score.py"
