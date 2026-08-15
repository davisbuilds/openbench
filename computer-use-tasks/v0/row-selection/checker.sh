#!/usr/bin/env bash
set -uo pipefail

python3 "$TASK_DIR/checker_data/verify.py" \
  "${OPENBENCH_CODEX_EVENTS_PATH:-$PWD/codex-events.jsonl}" \
  "${OPENBENCH_ATIF_PATH:-$PWD/trajectory.json}" \
  "${OPENBENCH_ROW_SELECTION_RESULT_PATH:-$PWD/artifacts/row-selection-result.json}"
