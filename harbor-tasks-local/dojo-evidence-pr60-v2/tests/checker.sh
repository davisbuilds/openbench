#!/usr/bin/env bash
set -euo pipefail
python_bin="${PR_EVAL_PYTHON:-python3}"
command -v "$python_bin" >/dev/null || { echo 'SKIP: Python interpreter unavailable (set PR_EVAL_PYTHON)' >&2; exit 77; }
"$python_bin" -I -c 'import yaml' >/dev/null 2>&1 || { echo 'SKIP: selected interpreter requires PyYAML' >&2; exit 77; }
exec "$python_bin" -I "${TASK_DIR:?TASK_DIR required}/checker_data/check.py"
