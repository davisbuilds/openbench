#!/usr/bin/env bash
# Scores engine.py against the formula grids. Prints a SCORE line and exits 0
# only when every cell matches. Runs with cwd = the agent's workspace copy; the
# scorer and grids live under $TASK_DIR/checker_data.
set -uo pipefail

exec python3 "$TASK_DIR/checker_data/run_score.py"
