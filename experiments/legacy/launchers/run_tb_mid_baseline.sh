#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
set +eu
set -a
source ~/.openbench/keys.env 2>/dev/null || true
set +a
set -eu
TASKS="adaptive-rejection-sampler,query-optimize,winning-avg-corewars,merge-diff-arc-agi-task,overfull-hbox,sanitize-git-repo"
PACK=".openbench/packs/openbench/tb-mid/1.0.0"
OUT="results/tb-mid-baseline-n3.jsonl"
for M in gpt-5.6-sol deepseek-v4-flash laguna-s-2.1 inkling; do
  python3 -m obench.run --harness pi --model "$M" --task "$TASKS" --tasks-dir "$PACK" --trials 3 --timeout 2400 --proxy --exec docker --results-path "$OUT" || true
done
touch results/tb-mid-baseline.DONE
