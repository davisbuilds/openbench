#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
ROOT="$PWD/results/grok45-matrix-20260718"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
for H in pi opencode; do
  python3 bench/run.py --harness $H --model grok-4.5 --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 1200 --exec docker --no-docker-fallback --results-path "$ROOT/grok45.jsonl"
done
touch "$ROOT/DONE"
