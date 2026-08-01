#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
set +eu; set -a; source ~/.openbench/keys.env 2>/dev/null || true; set +a; set -eu
G="$PWD/results/grok45-matrix-20260718"
:
M="$PWD/results/gpt56-matrix-20260718"
TB="terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval"
OPENBENCH_PROXY_LEDGER_DIR="$M/ledger" python3 bench/run.py --harness grokbuild --model gpt-5.6-sol --task "$TB" --tasks-dir tasks-imported --trials 3 --timeout 2400 --proxy --exec docker --no-docker-fallback --preflight-smoke --results-path "$M/gpt56.jsonl"
touch "$M/GB56-DONE"
