#!/usr/bin/env bash
# Scores the add-feature task: fraction of (new @include feature tests +
# existing regression groups) passing. Prints a `SCORE: <0.0-1.0>` line and
# exits 0 only when every feature test passes and no regression group is
# broken. Runs with cwd = the agent's workspace copy; the scoring harness and
# its hidden tests live under $TASK_DIR/checker_data.
set -uo pipefail

exec python3 "$TASK_DIR/checker_data/run_score.py"
