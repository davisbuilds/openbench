#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
set +eu; set -a; source ~/.openbench/keys.env 2>/dev/null || true; set +a; set -eu
M="$PWD/results/gpt56-matrix-20260718"
while [ ! -f "$M/CURSOR56-DONE" ]; do sleep 300; done
python3 - <<PY
import json
p="$M/gpt56.jsonl"
rows=[json.loads(l) for l in open(p)]
open(p+".oc-401.bak","w").write("".join(json.dumps(r)+"\n" for r in rows))
keep=[r for r in rows if not (r["harness"]=="opencode" and r["failure_class"]=="infra")]
print(len(rows),"->",len(keep))
open(p,"w").write("".join(json.dumps(r)+"\n" for r in keep))
PY
CORE="add-feature,build-a-cli,fix-failing-test,make-ci-green,make-it-run,misleading-error,taskflow,webcore"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
OPENBENCH_PROXY_LEDGER_DIR="$M/ledger" python3 bench/run.py --harness opencode --model gpt-5.6-sol --task "$CORE" --trials 3 --timeout 2400 --proxy --preflight-smoke --results-path "$M/gpt56.jsonl"
OPENBENCH_PROXY_LEDGER_DIR="$M/ledger" python3 bench/run.py --harness opencode --model gpt-5.6-sol --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 2400 --proxy --exec docker --no-docker-fallback --results-path "$M/gpt56.jsonl"
touch "$M/OC56-DONE"
