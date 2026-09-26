#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
set +eu
set -a
source ~/.openbench/keys.env 2>/dev/null || true
source ~/dev/openbench/keys.env 2>/dev/null || true
set +a
set -eu
ROOT="$PWD/results/laguna-inkling-20260721T193000Z"
mkdir -p "$ROOT"/ledger
CORE="add-feature,build-a-cli,fix-failing-test,make-ci-green,make-it-run,misleading-error,taskflow,webcore"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
run_pair() {
  OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness "$1" --model "$2" --task "$CORE" --trials 3 --timeout 1200 --proxy --results-path "$3"
  OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness "$1" --model "$2" --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 1200 --proxy --exec docker --results-path "$3"
}
for M in laguna-s-2.1 inkling; do
  for H in pi opencode grokbuild; do
    run_pair $H $M "$ROOT/laguna-inkling.jsonl"
  done
done
touch "$ROOT/DONE"
echo "$ROOT" > ~/dev/openbench/results/laguna-inkling-LATEST
