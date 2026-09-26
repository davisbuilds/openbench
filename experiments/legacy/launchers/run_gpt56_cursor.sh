#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
M="$PWD/results/gpt56-matrix-20260718"
while [ ! -f "$M/CLAUDE-DONE" ]; do sleep 300; done
CORE="add-feature,build-a-cli,fix-failing-test,make-ci-green,make-it-run,misleading-error,taskflow,webcore"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
python3 bench/run.py --harness cursor --model gpt-5.6-sol --task "$CORE" --trials 3 --timeout 2400 --exec docker --no-docker-fallback --results-path "$M/gpt56.jsonl"
python3 bench/run.py --harness cursor --model gpt-5.6-sol --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 2400 --exec docker --no-docker-fallback --results-path "$M/gpt56.jsonl"
touch "$M/CURSOR56-DONE"
