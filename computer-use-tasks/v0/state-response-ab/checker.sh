#!/usr/bin/env bash
set -uo pipefail

python3 "$TASK_DIR/checker_data/verify.py" "$OPENBENCH_FIXTURE_STATE_PATH"
