#!/usr/bin/env bash
# Wait for the final chain (grok kimi) to drain, then purge the exit-137 pi
# feal rows and rerun those 3 cells alone on the 12GB VM.
set -u
cd ~/dev/openbench
source ~/.openbench/keys.env
mkdir -p .bench-tmp logs
export TMPDIR=$PWD/.bench-tmp

while pgrep -f final_chain.sh >/dev/null; do sleep 60; done
echo "=== final chain drained; purging feal-137 rows ==="

python3 purge_feal137.py

echo "=== pi deepseek feal retry (solo, 12GB) ==="
python3 bench/run.py --task terminal-bench/feal-differential-cryptanalysis --harness pi --model deepseek-v4-flash \
  --trials 3 --tasks-dir tasks-imported --exec docker --no-docker-fallback \
  --timeout 1200 --checker-timeout 300 \
  --results-path results/tb-open-n3-deepseek-v4-flash.jsonl
echo "=== feal retry complete ==="
