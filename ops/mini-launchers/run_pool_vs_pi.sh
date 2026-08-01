#!/usr/bin/env bash
# Pool x laguna coverage completion, bracketed by pi control probes so we can
# test whether provider conditions drifted during pool window (429 rates have
# been observed swinging 0->53% between windows).
set -uo pipefail
cd ~/dev/openbench
set -a
source ~/.openbench/keys.env 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"
export POOLSIDE_API_KEY="${OPENROUTER_API_KEY:-}"
export POOLSIDE_STANDALONE_BASE_URL="https://openrouter.ai/api/v1"
set +a
PACK=".openbench/packs/openbench/tb-mid/1.0.0"
TASKS="adaptive-rejection-sampler query-optimize merge-diff-arc-agi-task overfull-hbox sanitize-git-repo"
OUT=results/pool-vs-pi-n3.jsonl
L=results/pool-vs-pi-ledger
mkdir -p $L

probe() { # label trial
  local label=$1 tr=$2
  for T in $TASKS; do
    OPENBENCH_PROXY_LEDGER_DIR=$L python3 -m obench.run --force --harness pi --model laguna-s-2.1 \
      --task "$T" --tasks-dir "$PACK" --trial "$tr" --trials 3 --timeout 2400 --proxy --exec docker \
      --results-path "results/pi-control-${label}.jsonl" >/dev/null 2>&1
    echo "  control-$label $T done"
    sleep 30
  done
}

echo "=== PRE control probe (pi x laguna, 5 cells)"
probe pre 1
echo "=== POOL arm: 5 tasks x 3 trials, retry on 429"
for T in $TASKS; do
  for tr in 1 2 3; do
    for attempt in 1 2 3 4; do
      OPENBENCH_PROXY_LEDGER_DIR=$L python3 -m obench.run --force \
        --candidate experiments/candidates/pool.toml --model laguna-s-2.1 \
        --task "$T" --tasks-dir "$PACK" --trial "$tr" --trials 3 --timeout 2400 --proxy --exec docker \
        --results-path "$OUT" >/dev/null 2>&1
      if python3 - "$OUT" "$T" "$tr" <<PY
import json,sys
p,t,tr=sys.argv[1:4]
ok=False
for l in open(p):
    r=json.loads(l)
    if r["task"]==t and str(r["trial"])==tr and r["failure_class"] not in ("infra","rate_limited","stalled"):
        ok=True
sys.exit(0 if ok else 1)
PY
      then echo "OK   pool/$T/t$tr (attempt $attempt)"; break; fi
      echo "RETRY pool/$T/t$tr attempt $attempt"; sleep $((attempt*300))
    done
  done
done
echo "=== POST control probe (pi x laguna, 5 cells)"
probe post 2
touch results/poolvspi.DONE
