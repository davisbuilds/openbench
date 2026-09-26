#!/usr/bin/env bash
set -uo pipefail
cd ~/dev/openbench
set -a
source ~/.openbench/keys.env 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"
export POOLSIDE_API_KEY="${OPENROUTER_API_KEY:-}"
export POOLSIDE_STANDALONE_BASE_URL="https://openrouter.ai/api/v1"
set +a
for T in adaptive-rejection-sampler query-optimize merge-diff-arc-agi-task overfull-hbox sanitize-git-repo; do
  for TR in 1 2 3; do
    python3 -m obench.run --candidate experiments/candidates/pool.toml --model laguna-s-2.1 --task "$T" --tasks-dir .openbench/packs/openbench/tb-mid/1.0.0 --trial $TR --trials 3 --timeout 2400 --proxy --exec docker --results-path results/tb-mid-baseline-n3.jsonl || true
    sleep 90
  done
done
touch results/poolstagger.DONE
