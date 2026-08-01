#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
set +eu
set -a
source ~/.openbench/keys.env 2>/dev/null || true
set +a
set -eu
ROOT="$PWD/results/grokbuild-cross-20260716T134744Z"
while [ ! -f "$ROOT/DONE" ]; do sleep 300; done
CORE="add-feature,build-a-cli,fix-failing-test,make-ci-green,make-it-run,misleading-error,taskflow,webcore"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
strip_arm() {
  python3 - "$1" <<PY
import json,sys
p="$ROOT/cross-deepseek.jsonl"; arm=sys.argv[1]
rows=[json.loads(l) for l in open(p)]
keep=[r for r in rows if r["harness"]!=arm]
open(p+f".pre-{arm}-meter.bak","w").write("".join(json.dumps(r)+"\n" for r in rows))
open(p,"w").write("".join(json.dumps(r)+"\n" for r in keep))
print(arm, len(rows), "->", len(keep))
PY
}
run_pair() {
  OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness "$1" --model deepseek-v4-flash --task "$CORE" --trials 3 --timeout 1200 --proxy --results-path "$ROOT/cross-deepseek.jsonl"
  OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness "$1" --model deepseek-v4-flash --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 1200 --proxy --exec docker --results-path "$ROOT/cross-deepseek.jsonl"
}
strip_arm pi
run_pair pi
nohup bash bench/openmodel_bridge.sh > "$ROOT/bridge2.log" 2>&1 &
BRIDGE_PID=$!
for i in $(seq 1 30); do
  if curl -s -o /dev/null http://localhost:4141/health 2>/dev/null; then break; fi
  sleep 2
done
strip_arm codex
run_pair codex
kill $BRIDGE_PID 2>/dev/null || true
touch "$ROOT/METERED-DONE"
