#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
set +eu
set -a
source ~/dev/openbench/keys.env 2>/dev/null || true
source ~/.openbench/keys.env 2>/dev/null || true
set +a
set -eu
TOGGLE=/Users/matthewlam/dev/openbench/results/multiagent-toggle-20260716T010112Z
while [ ! -f "$TOGGLE/DONE" ]; do sleep 300; done
ROOT="$PWD/results/grokbuild-cross-20260716T134744Z"
mkdir -p "$ROOT"/ledger
CORE="add-feature,build-a-cli,fix-failing-test,make-ci-green,make-it-run,misleading-error,taskflow,webcore"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
run_pair() {
  OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness "$1" --model "$2" --task "$CORE" --trials 3 --timeout 1200 --proxy --results-path "$3"
  OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness "$1" --model "$2" --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 1200 --proxy --exec docker --results-path "$3"
}
for H in grokbuild pi opencode claude; do
  run_pair $H deepseek-v4-flash "$ROOT/cross-deepseek.jsonl"
done
nohup bash bench/openmodel_bridge.sh > "$ROOT/bridge.log" 2>&1 &
BRIDGE_PID=$!
sleep 20
run_pair codex deepseek-v4-flash "$ROOT/cross-deepseek.jsonl"
kill $BRIDGE_PID 2>/dev/null || true
touch "$ROOT/DONE"
echo "$ROOT" > ~/dev/openbench/results/grokbuild-cross-LATEST
