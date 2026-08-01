#!/usr/bin/env bash
# Launch the tb-mid refill queue with the API keys actually in the environment.
# The first attempt was launched WITHOUT sourcing keys.env and burned the whole
# retry budget for a cell in 90s on "SETUP-NEEDED: export DEEPSEEK_API_KEY" --
# an env error that the queue classified as infra and retried rather than
# failing loudly. Hence the explicit precondition check below.
set -uo pipefail
cd ~/dev/openbench
set -a
source ~/.openbench/keys.env
set +a
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  echo "ABORT: OPENROUTER_API_KEY not in environment; laguna+inkling cells would all fail as infra" >&2
  exit 1
fi
echo "precondition OK: OPENROUTER_API_KEY present"
rm -f results/tb-mid-refill/ledger/queue-state.json
exec python3 -m obench.matrix_queue --spec tb-mid-refill.toml
