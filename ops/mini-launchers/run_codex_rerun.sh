#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
set +eu
set -a
source ~/.openbench/keys.env 2>/dev/null || true
set +a
set -eu
ROOT="$PWD/results/grokbuild-cross-20260716T134744Z"
CORE="add-feature,build-a-cli,fix-failing-test,make-ci-green,make-it-run,misleading-error,taskflow,webcore"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
nohup bash bench/openmodel_bridge.sh > "$ROOT/bridge.log" 2>&1 &
BRIDGE_PID=$!
for i in $(seq 1 30); do
  if curl -s -o /dev/null http://localhost:4141/health 2>/dev/null; then break; fi
  sleep 2
done
OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness codex --model deepseek-v4-flash --task "$CORE" --trials 3 --timeout 1200 --proxy --results-path "$ROOT/cross-deepseek.jsonl"
OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness codex --model deepseek-v4-flash --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 1200 --proxy --exec docker --results-path "$ROOT/cross-deepseek.jsonl"
kill $BRIDGE_PID 2>/dev/null || true
touch "$ROOT/DONE"
