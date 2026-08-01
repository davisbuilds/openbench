#!/usr/bin/env bash
# Re-run deepseek under corrected model limits. Mini: OpenRouter key lives there.
# exists nowhere else, and a launch without it burns each cell's retry budget
# in seconds on "SETUP-NEEDED" (which the queue classifies as infra).
set -uo pipefail
cd "$(dirname "$0")"
set -a; source ~/.openbench/keys.env; set +a
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  echo "ABORT: OPENROUTER_API_KEY missing" >&2; exit 1
fi
echo "precondition OK: OPENROUTER_API_KEY present"
exec python3 -m obench.matrix_queue --spec rerun-laguna.toml
