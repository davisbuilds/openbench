#!/usr/bin/env bash
set -uo pipefail
exec python3 "$TASK_DIR/checker_data/run_score.py"
