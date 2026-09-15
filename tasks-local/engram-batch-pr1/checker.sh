#!/usr/bin/env bash
set -euo pipefail
command -v python3 >/dev/null || { echo 'SKIP: python3 required' >&2; exit 77; }
exec python3 "${TASK_DIR:?TASK_DIR required}/checker_data/check.py"
