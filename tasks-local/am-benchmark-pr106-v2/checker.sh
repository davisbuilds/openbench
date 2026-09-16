#!/usr/bin/env bash
set -euo pipefail
: "${TASK_DIR:?TASK_DIR must identify the task root}"
exec python3 "$TASK_DIR/checker_data/check.py" "$PWD" "$TASK_DIR"
