#!/usr/bin/env bash
# Terminal-Bench 'gcode-to-text' checker (adapted for OpenBench).
# cwd = a fresh copy of workspace/ (text.gcode plus the agent's out.txt, if any).
# Exit 0 => out.txt matches the checker-owned decoded text and text.gcode still
# matches the checker-owned input artifact.
set -uo pipefail
exec python3 "$TASK_DIR/checker_data/run_checks.py"
