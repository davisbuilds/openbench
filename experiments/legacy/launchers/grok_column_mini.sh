#!/usr/bin/env bash
# Resume the grok column on the mini. NO purge — run.py skips existing run_ids.
set -u
cd ~/dev/openbench
source ~/.openbench/keys.env
mkdir -p .bench-tmp logs
export TMPDIR=$PWD/.bench-tmp

TASKS=terminal-bench/cancel-async-tasks,terminal-bench/feal-differential-cryptanalysis,terminal-bench/llm-inference-batching-scheduler,terminal-bench/schemelike-metacircular-eval
for model in deepseek-v4-flash glm-5.2 kimi-k2.7-code; do
  echo "=== grokbuild x $model (mini resume) ==="
  python3 bench/run.py --task "$TASKS" --harness grokbuild --model "$model" \
    --trials 3 --tasks-dir tasks-imported --exec docker --no-docker-fallback \
    --timeout 1200 --checker-timeout 300 \
    --results-path "results/tb-open-n3-${model}.jsonl"
done
echo "=== grok column mini resume complete ==="
