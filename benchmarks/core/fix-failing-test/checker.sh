#!/usr/bin/env bash
# Runs with cwd set to a fresh copy of the task workspace.
# Exit 0 => task solved (all unit tests pass), nonzero => failed.
set -euo pipefail

python3 -m unittest -v
