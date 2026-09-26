#!/usr/bin/env bash
# Scores store.py against the scripted transaction sessions. Prints a SCORE line
# and exits 0 only when every script's output matches exactly. Runs with cwd =
# the agent's workspace copy; the scorer and scripts live under
# $TASK_DIR/checker_data.
set -uo pipefail

exec python3 "$TASK_DIR/checker_data/run_score.py"
