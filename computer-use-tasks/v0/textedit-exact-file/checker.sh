#!/usr/bin/env bash
set -uo pipefail

python3 "$TASK_DIR/checker_data/verify.py" "$PWD" "${OPENBENCH_NATIVE_OUTPUT_PATH:-}"
