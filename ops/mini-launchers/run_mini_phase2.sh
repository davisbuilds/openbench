#!/usr/bin/env bash
set -uo pipefail
cd ~/dev/openbench
set -a
source ~/.openbench/keys.env 2>/dev/null || true
export POOLSIDE_API_KEY="${OPENROUTER_API_KEY:-}"
export PATH="$HOME/.local/bin:$PATH"
set +a
MIDTASKS="adaptive-rejection-sampler,query-optimize,winning-avg-corewars,merge-diff-arc-agi-task,overfull-hbox,sanitize-git-repo"
MIDPACK=".openbench/packs/openbench/tb-mid/1.0.0"
MIDOUT="results/tb-mid-baseline-n3.jsonl"
CORE="add-feature,build-a-cli,fix-failing-test,make-ci-green,make-it-run,misleading-error,taskflow,webcore"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
MATRIXOUT="results/laguna-inkling-20260721T193000Z/laguna-inkling.jsonl"
LEDGER="results/laguna-inkling-20260721T193000Z/ledger"
# 1) matrix top-off: rerun missing + previously excluded cells (run_id skip covers done cells)
for H in pi opencode grokbuild; do
  for M in laguna-s-2.1 inkling; do
    OPENBENCH_PROXY_LEDGER_DIR="$LEDGER" python3 bench/run.py --harness $H --model $M --task "$CORE" --trials 3 --timeout 1200 --proxy --results-path "$MATRIXOUT" || true
    OPENBENCH_PROXY_LEDGER_DIR="$LEDGER" python3 bench/run.py --harness $H --model $M --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 1200 --proxy --exec docker --results-path "$MATRIXOUT" || true
  done
done
touch results/laguna-inkling-20260721T193000Z/DONE
# 2) tb-mid baseline
for M in gpt-5.6-sol deepseek-v4-flash laguna-s-2.1 inkling; do
  python3 -m obench.run --harness pi --model "$M" --task "$MIDTASKS" --tasks-dir "$MIDPACK" --trials 3 --timeout 2400 --proxy --exec docker --results-path "$MIDOUT" || true
done
# 3) pool arms
python3 -m obench.run --candidate experiments/candidates/pool.toml --model laguna-s-2.1 --task "$MIDTASKS" --tasks-dir "$MIDPACK" --trials 3 --timeout 2400 --proxy --exec docker --results-path "$MIDOUT" || true
python3 -m obench.run --candidate experiments/candidates/pool.toml --model laguna-s-2.1 --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 1200 --proxy --exec docker --results-path results/pool-laguna-tb7-n3.jsonl || true
touch results/phase2.DONE
