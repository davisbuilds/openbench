#!/usr/bin/env bash
# Terminal-Bench 'schemelike-metacircular-eval' checker (adapted for OpenBench).
# cwd = a fresh copy of workspace/ (interp.py, test/, shadow_test/, and the
# agent's eval.scm). Exit 0 => running every test program through eval.scm
# (and eval.scm through itself, for a few programs) reproduces the output of
# running it directly through interp.py.
set -uo pipefail
exec python3 "$TASK_DIR/checker_data/run_checks.py"
