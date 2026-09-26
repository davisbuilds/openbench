#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
set +eu
set -a
source ~/dev/openbench/keys.env 2>/dev/null || true
source ~/.openbench/keys.env 2>/dev/null || true
set +a
set -eu
ROOT="$PWD/results/multiagent-toggle-20260716T010112Z"
mkdir -p "$ROOT"/{ledger/off-core,ledger/off-tb,ledger/on-core,ledger/on-tb,prefix}
CORE="add-feature,build-a-cli,fix-failing-test,make-ci-green,make-it-run,misleading-error,taskflow,webcore"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger/off-core" python3 bench/run.py --harness codex --model gpt-5.6-sol --task "$CORE" --trials 5 --timeout 1200 --proxy --results-path "$ROOT/codex-off.jsonl"
OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger/off-tb" python3 bench/run.py --harness codex --model gpt-5.6-sol --task "$TB" --tasks-dir tasks-imported --trials 5 --timeout 1200 --proxy --exec docker --results-path "$ROOT/codex-off.jsonl"
OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger/on-core" python3 bench/run.py --candidate experiments/multiagent-toggle/codex-on.toml --model gpt-5.6-sol --task "$CORE" --trials 5 --timeout 1200 --proxy --results-path "$ROOT/codex-on.jsonl"
OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger/on-tb" python3 bench/run.py --candidate experiments/multiagent-toggle/codex-on.toml --model gpt-5.6-sol --task "$TB" --tasks-dir tasks-imported --trials 5 --timeout 1200 --proxy --exec docker --results-path "$ROOT/codex-on.jsonl"
python3 bench/stats.py --strict-provenance --min-n 75 --tasks-dir tasks --tasks-dir tasks-imported "$ROOT/codex-off.jsonl" "$ROOT/codex-on.jsonl" | tee "$ROOT/stats.txt"
python3 bench/report.py --efficiency --results-path "$ROOT/codex-off.jsonl" | tee "$ROOT/off-efficiency.txt"
python3 bench/report.py --efficiency --results-path "$ROOT/codex-on.jsonl" | tee "$ROOT/on-efficiency.txt"
for arm in off-core off-tb on-core on-tb; do
  for ledger in "$ROOT/ledger/$arm"/*.jsonl; do
    [ -e "$ledger" ] || continue
    cell=$(basename "$ledger" .jsonl)
    python3 bench/analyze_prefix.py "$ledger" > "$ROOT/prefix/$arm-$cell.json"
  done
done
touch "$ROOT/DONE"
echo "$ROOT" > ~/dev/openbench/results/multiagent-toggle-LATEST
