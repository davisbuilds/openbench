#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
set +eu; set -a; source ~/.openbench/keys.env 2>/dev/null || true; set +a; set -eu
M=/Users/matthewlam/dev/openbench/results/grok45-matrix-20260718
while [ ! -f "$M/DONE" ]; do sleep 300; done
ROOT="$PWD/results/kimi-k3-rescue-2400s"
mkdir -p "$ROOT/ledger"
run() {
  OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness "$1" --model kimi-k3 --task "$2" --tasks-dir tasks-imported --trials 3 --timeout 2400 --proxy --exec docker --results-path "$ROOT/rescue.jsonl"
}
run pi        "terminal-bench/gcode-to-text,terminal-bench/raman-fitting"
run opencode  "terminal-bench/gcode-to-text,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
run grokbuild "terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
touch "$ROOT/DONE"
