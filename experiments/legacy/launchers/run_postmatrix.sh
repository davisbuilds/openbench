#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
set +eu; set -a; source ~/.openbench/keys.env 2>/dev/null || true; set +a; set -eu
M="$PWD/results/gpt56-matrix-20260718"
while [ ! -f "$M/DONE" ]; do sleep 300; done
git pull -q
docker build -t openbench-harness:latest bench/docker > /tmp/postmatrix-build.log 2>&1
# purge claude infra rows (proxy-route bug) and rerun claude arm
python3 - <<PY
import json
p="$M/gpt56.jsonl"
rows=[json.loads(l) for l in open(p)]
open(p+".claude-noroute.bak","w").write("".join(json.dumps(r)+"\n" for r in rows))
keep=[r for r in rows if not (r["harness"]=="claude" and r["failure_class"]=="infra")]
print(len(rows),"->",len(keep))
open(p,"w").write("".join(json.dumps(r)+"\n" for r in keep))
PY
CORE="add-feature,build-a-cli,fix-failing-test,make-ci-green,make-it-run,misleading-error,taskflow,webcore"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
OPENBENCH_PROXY_LEDGER_DIR="$M/ledger" python3 bench/run.py --harness claude --model gpt-5.6-sol --task "$CORE" --trials 3 --timeout 2400 --proxy --results-path "$M/gpt56.jsonl"
OPENBENCH_PROXY_LEDGER_DIR="$M/ledger" python3 bench/run.py --harness claude --model gpt-5.6-sol --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 2400 --proxy --exec docker --no-docker-fallback --results-path "$M/gpt56.jsonl"
touch "$M/CLAUDE-DONE"
# kimi rescue 2 with calm-probe gate
OKS=0
while [ "$OKS" -lt 3 ]; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" https://api.moonshot.ai/v1/chat/completions -H "Authorization: Bearer $MOONSHOT_API_KEY" -H "Content-Type: application/json" -d '{"model":"kimi-k3","messages":[{"role":"user","content":"reply ok"}],"max_tokens":256,"thinking":{"type":"enabled"}}' || echo 000)
  if [ "$CODE" = "200" ]; then OKS=$((OKS+1)); else OKS=0; fi
  echo "$(date -u +%H:%M:%SZ) rescue2-probe $CODE oks=$OKS"
  if [ "$OKS" -lt 3 ]; then sleep 120; fi
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
