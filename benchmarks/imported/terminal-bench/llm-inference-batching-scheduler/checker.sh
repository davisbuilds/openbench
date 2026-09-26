#!/usr/bin/env bash
# Terminal-Bench 'llm-inference-batching-scheduler' checker (adapted for OpenBench).
# cwd = a fresh copy of workspace/ (task_file/input_data/*.jsonl untouched, plus
# the agent's task_file/output_data/plan_b1.jsonl and plan_b2.jsonl).
# Exit 0 => plans cover all requests, satisfy schema/shape/batch constraints, keep
# input data unmodified, and meet the performance thresholds.
set -uo pipefail
exec python3 "$TASK_DIR/checker_data/run_checks.py"
