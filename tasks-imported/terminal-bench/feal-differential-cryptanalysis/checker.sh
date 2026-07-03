#!/usr/bin/env bash
# Terminal-Bench 'feal-differential-cryptanalysis' checker (adapted for OpenBench).
# cwd = a fresh copy of workspace/ (contains feal.py, plus the agent's attack.py).
# Exit 0 => attack.attack(feal.encrypt) recovers key[5] across all trials.
#
# NOTE: the upstream test verifies the attack against a FEAL implementation
# compiled from C (feal_module.c via setup.py build_ext). The openbench-harness
# image has no compiler, so this checker verifies the attack against the
# reference pure-Python feal.py instead (identical cipher; see PROVENANCE.md).
set -uo pipefail
exec python3 "$TASK_DIR/checker_data/run_checks.py"
