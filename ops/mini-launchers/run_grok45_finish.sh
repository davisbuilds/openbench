#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
ROOT="$PWD/results/grok45-matrix-20260718"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
python3 bench/run.py --harness grokbuild --model grok-4.5 --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 2400 --exec docker --no-docker-fallback --preflight-smoke --results-path "$ROOT/grok45.jsonl"
touch "$ROOT/GB-TB-DONE2"
