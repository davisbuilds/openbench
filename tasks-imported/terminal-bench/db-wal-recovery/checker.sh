#!/usr/bin/env bash
# Terminal-Bench 'db-wal-recovery' checker (adapted for OpenBench).
# cwd = a fresh copy of workspace/ (with the agent's recovered.json, if any).
# Exit 0 => recovered.json exactly matches the checker-owned recovered rows.
set -uo pipefail
exec python3 "$TASK_DIR/checker_data/run_checks.py"
