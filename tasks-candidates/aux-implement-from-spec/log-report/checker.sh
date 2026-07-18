#!/usr/bin/env bash
# Scores report.py against a set of command invocations. Prints a SCORE line and
# exits 0 only when every command's output matches exactly. Runs with cwd = the
# agent's workspace copy; the scorer and log data live under
# $TASK_DIR/checker_data.
set -uo pipefail

exec python3 "$TASK_DIR/checker_data/run_score.py"
