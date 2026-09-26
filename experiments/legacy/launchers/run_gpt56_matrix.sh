#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
set +eu; set -a; source ~/.openbench/keys.env 2>/dev/null || true; set +a; set -eu
G=/Users/matthewlam/dev/openbench/results/grok45-matrix-20260718
while [ ! -f "$G/GB-TB-DONE" ]; do sleep 300; done
ROOT="$PWD/results/gpt56-matrix-20260718"
mkdir -p "$ROOT/ledger"
CORE="add-feature,build-a-cli,fix-failing-test,make-ci-green,make-it-run,misleading-error,taskflow,webcore"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
run_pair() {
  OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness "$1" --model gpt-5.6-sol --task "$CORE" --trials 3 --timeout 2400 --proxy --results-path "$ROOT/gpt56.jsonl"
  OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness "$1" --model gpt-5.6-sol --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 2400 --proxy --exec docker --no-docker-fallback --results-path "$ROOT/gpt56.jsonl"
}
for H in codex pi opencode claude grokbuild; do
  run_pair $H
done
touch "$ROOT/DONE"
