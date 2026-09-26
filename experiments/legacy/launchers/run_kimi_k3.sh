#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
set +eu
set -a
source ~/.openbench/keys.env 2>/dev/null || true
set +a
set -eu
CROSS=/Users/matthewlam/dev/openbench/results/grokbuild-cross-20260716T134744Z
while [ ! -f "$CROSS/DONE" ]; do sleep 300; done
SMOKEROOT=/Users/matthewlam/dev/openbench/results/grok45-smoke
# grok45 smoke is short; wait for its DONE too if the script created a root
for i in $(seq 1 60); do
  if [ -f "$SMOKEROOT/DONE" ]; then break; fi
  sleep 60
done
# kimi-k3 launch-day saturation: wait for 3 consecutive OK probes 120s apart
OKS=0
while [ "$OKS" -lt 3 ]; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" https://api.moonshot.ai/v1/chat/completions -H "Authorization: Bearer $MOONSHOT_API_KEY" -H "Content-Type: application/json" -d '{"model":"kimi-k3","messages":[{"role":"user","content":"reply ok"}],"max_tokens":256,"thinking":{"type":"enabled"}}' || echo 000)
  if [ "$CODE" = "200" ]; then OKS=$((OKS+1)); else OKS=0; fi
  echo "$(date -u +%H:%M:%SZ) kimi-probe $CODE oks=$OKS"
  if [ "$OKS" -lt 3 ]; then sleep 120; fi
done
ROOT="$PWD/results/kimi-k3-20260717T053000Z"
mkdir -p "$ROOT"/ledger
CORE="add-feature,build-a-cli,fix-failing-test,make-ci-green,make-it-run,misleading-error,taskflow,webcore"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
run_pair() {
  OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness "$1" --model kimi-k3 --task "$CORE" --trials 3 --timeout 1200 --proxy --results-path "$2"
  OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness "$1" --model kimi-k3 --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 1200 --proxy --exec docker --results-path "$2"
}
for H in pi opencode grokbuild; do
  run_pair $H "$ROOT/kimi-k3.jsonl"
done
touch "$ROOT/DONE"
echo "$ROOT" > ~/dev/openbench/results/kimi-k3-LATEST
