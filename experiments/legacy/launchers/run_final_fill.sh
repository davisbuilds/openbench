#!/usr/bin/env bash
set -uo pipefail
cd ~/dev/openbench
set -a
source ~/.openbench/keys.env 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"
export POOLSIDE_API_KEY="${OPENROUTER_API_KEY:-}"
export POOLSIDE_STANDALONE_BASE_URL="https://openrouter.ai/api/v1"
set +a
T5="adaptive-rejection-sampler,query-optimize,merge-diff-arc-agi-task,overfull-hbox,sanitize-git-repo"
PACK=".openbench/packs/openbench/tb-mid/1.0.0"
OUT="results/tb-mid-baseline-n3.jsonl"
# gpt done
# python3 -m obench.run --harness pi --model gpt-5.6-sol --task "$T5" --tasks-dir "$PACK" --trials 3 --timeout 2400 --proxy --exec docker --results-path "$OUT" || true
python3 -m obench.run --candidate experiments/candidates/pool.toml --model laguna-s-2.1 --task "$T5" --tasks-dir "$PACK" --trials 3 --timeout 2400 --proxy --exec docker --results-path "$OUT" || true
touch results/finalfill.DONE
