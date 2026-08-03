#!/usr/bin/env bash
# Re-run laguna under corrected model limits. The Mini holds the OpenRouter key;
# a launch without it burns each cell's retry budget in seconds on
# "SETUP-NEEDED" (which the queue classifies as infra).
set -uo pipefail
REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)" || {
  echo "ABORT: cannot determine repository root" >&2; exit 1;
}
[ -n "$REPO_ROOT" ] || { echo "ABORT: repository root is empty" >&2; exit 1; }
cd "$REPO_ROOT" || { echo "ABORT: cannot enter repository root: $REPO_ROOT" >&2; exit 1; }
set -a; source ~/.openbench/keys.env; set +a
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  echo "ABORT: OPENROUTER_API_KEY missing" >&2; exit 1
fi
echo "precondition OK: OPENROUTER_API_KEY present"
exec python3 -m obench.matrix_queue --spec experiments/specs/corrected-model-limits-laguna.toml
