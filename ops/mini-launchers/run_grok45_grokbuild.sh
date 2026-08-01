#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
ROOT="$PWD/results/grok45-matrix-20260718"
while [ ! -f "$ROOT/CURSOR-DONE" ]; do sleep 300; done
CORE="add-feature,build-a-cli,fix-failing-test,make-ci-green,make-it-run,misleading-error,taskflow,webcore"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
python3 bench/run.py --harness grokbuild --model grok-4.5 --task "$CORE" --trials 3 --timeout 1200 --results-path "$ROOT/grok45.jsonl"
python3 bench/run.py --harness grokbuild --model grok-4.5 --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 1200 --exec docker --results-path "$ROOT/grok45.jsonl"
touch "$ROOT/GB-DONE"
