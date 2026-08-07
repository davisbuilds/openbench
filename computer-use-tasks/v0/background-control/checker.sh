#!/usr/bin/env bash
set -uo pipefail

state_path="${OPENBENCH_FIXTURE_STATE_PATH:-$PWD/artifacts/background-control-state.json}"
ledger_path="${OPENBENCH_FOCUS_LEDGER_PATH:-$PWD/artifacts/focus-ledger.jsonl}"
seal_path="${OPENBENCH_FOCUS_SEAL_PATH:-$PWD/artifacts/focus-seal.json}"
python3 "$TASK_DIR/checker_data/verify.py" "$state_path" "$ledger_path" "$seal_path"
