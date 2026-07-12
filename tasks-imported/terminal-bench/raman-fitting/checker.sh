#!/usr/bin/env bash
# Terminal-Bench 'raman-fitting' checker (adapted for OpenBench).
# cwd = a fresh copy of workspace/ (graphene.dat plus the agent's results.json,
# if any). Exit 0 => results.json matches checker-owned fit parameters within
# the upstream tolerances, and the input spectrum was not tampered with.
set -uo pipefail
exec python3 "$TASK_DIR/checker_data/run_checks.py"
