#!/usr/bin/env bash
# Refill 429-dropped cells to full n=3 coverage. Staggered: 429s hit long cells,
# so pace requests and retry each cell up to 3 times.
set -uo pipefail
cd ~/dev/openbench
set -a
source ~/.openbench/keys.env 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"
set +a
CORE="add-feature,build-a-cli,fix-failing-test,make-ci-green,make-it-run,misleading-error,taskflow,webcore"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
ROOT=results/laguna-inkling-20260721T193000Z
OUT=$ROOT/laguna-inkling.jsonl
for PASS in 1 2 3; do
  echo "=== coverage pass $PASS"
  for M in inkling laguna-s-2.1; do
    for H in pi opencode grokbuild; do
      OPENBENCH_PROXY_LEDGER_DIR=$ROOT/ledger python3 bench/run.py --harness $H --model $M --task "$CORE" --trials 3 --timeout 1200 --proxy --results-path "$OUT" || true
      sleep 45
      OPENBENCH_PROXY_LEDGER_DIR=$ROOT/ledger python3 bench/run.py --harness $H --model $M --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 1200 --proxy --exec docker --results-path "$OUT" || true
      sleep 45
    done
  done
done
touch results/coverage.DONE
