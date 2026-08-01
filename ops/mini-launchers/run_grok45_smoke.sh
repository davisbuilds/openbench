#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
CROSS=/Users/matthewlam/dev/openbench/results/grokbuild-cross-20260716T134744Z
while [ ! -f "$CROSS/DONE" ]; do sleep 300; done
ROOT="$PWD/results/grok45-smoke"
mkdir -p "$ROOT/ledger"
for H in grokbuild pi opencode; do
  OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness $H --model grok-4.5 --task make-it-run --trials 1 --timeout 1200 --proxy --results-path "$ROOT/smoke.jsonl" || true
done
touch "$ROOT/DONE"
