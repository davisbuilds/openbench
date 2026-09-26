#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
set +eu
set -a
source ~/.openbench/keys.env 2>/dev/null || true
set +a
set -eu
TASKS="dna-insert,polyglot-rust-c,filter-js-from-html,model-extraction-relu-logits,regex-chess,sam-cell-seg,torch-pipeline-parallelism,torch-tensor-parallelism,video-processing"
PACK=".openbench/packs/openbench/tb-hard/1.0.0"
OUT="results/tb-hard-disc-n3.jsonl"
for M in gpt-5.6-sol deepseek-v4-flash inkling; do
  python3 -m obench.run --harness pi --model "$M" --task "$TASKS" --tasks-dir "$PACK" --trials 3 --timeout 2400 --proxy --exec docker --results-path "$OUT"
done
touch results/tb-hard-disc.DONE
