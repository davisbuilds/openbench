#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
set +eu; set -a; source ~/.openbench/keys.env 2>/dev/null || true; set +a; set -eu
M="$PWD/results/gpt56-matrix-20260718"
while [ ! -f "$M/GB56-DONE" ]; do sleep 300; done
OKS=0
while [ "$OKS" -lt 3 ]; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" https://api.moonshot.ai/v1/chat/completions -H "Authorization: Bearer $MOONSHOT_API_KEY" -H "Content-Type: application/json" -d '{"model":"kimi-k3","messages":[{"role":"user","content":"reply ok"}],"max_tokens":256,"thinking":{"type":"enabled"}}' || echo 000)
  if [ "$CODE" = "200" ]; then OKS=$((OKS+1)); else OKS=0; fi
  echo "$(date -u +%H:%M:%SZ) rescue-probe $CODE oks=$OKS"
  if [ "$OKS" -lt 3 ]; then sleep 300; fi
done
ROOT="$PWD/results/kimi-k3-rescue2-2400s"
mkdir -p "$ROOT/ledger"
run() {
  OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness "$1" --model kimi-k3 --task "$2" --tasks-dir tasks-imported --trials 3 --timeout 2400 --proxy --exec docker --results-path "$ROOT/rescue2.jsonl"
}
run opencode  "terminal-bench/gcode-to-text,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
run grokbuild "terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
run pi        "terminal-bench/gcode-to-text,terminal-bench/raman-fitting"
touch "$ROOT/DONE"
