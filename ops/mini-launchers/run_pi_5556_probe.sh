#!/usr/bin/env bash
set -euo pipefail
cd ~/dev/openbench
G="$PWD/results/grok45-matrix-20260718"
while [ ! -f "$G/GB-TB-DONE2" ]; do sleep 300; done
ROOT="$PWD/results/pi-5556-probe-20260720"
mkdir -p "$ROOT/ledger"
HARD="terminal-bench/feal-differential-cryptanalysis,terminal-bench/extract-elf,terminal-bench/raman-fitting"
for M in gpt-5.5-medium gpt-5.6-sol; do
  OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger" python3 bench/run.py --harness pi --model $M --task "$HARD" --tasks-dir tasks-imported --trials 5 --timeout 2400 --proxy --exec docker --no-docker-fallback --preflight-smoke --results-path "$ROOT/probe.jsonl"
done
touch "$ROOT/DONE"
