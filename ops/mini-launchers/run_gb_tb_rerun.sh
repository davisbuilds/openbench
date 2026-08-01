#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
R=/Users/matthewlam/dev/openbench/results/kimi-k3-rescue-2400s
while [ ! -f "$R/DONE" ]; do sleep 300; done
ROOT="$PWD/results/grok45-matrix-20260718"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
python3 bench/run.py --harness grokbuild --model grok-4.5 --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 1200 --exec docker --results-path "$ROOT/grok45.jsonl"
touch "$ROOT/GB-TB-DONE"
