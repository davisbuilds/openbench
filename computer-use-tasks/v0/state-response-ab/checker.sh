#!/usr/bin/env bash
set -uo pipefail

state_path="${OPENBENCH_FIXTURE_STATE_PATH:-$PWD/artifacts/state-response-ab-state.json}"
python3 "$TASK_DIR/checker_data/verify.py" "$state_path"
