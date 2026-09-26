#!/usr/bin/env bash
# Close pool x laguna's last 4 tb-mid cells: overfull-hbox#t1 (its only rows are
# rate_limited/infra) and all three winning-avg-corewars trials (never run).
#
# Not driven by obench.matrix_queue on purpose: the queue always emits
# --harness, and a BYO candidate's run_id is namespaced by its spec digest
# (make_run_id's candidate_digest -> "pool@5ef7d7096f32:..."). Queueing it would
# write cells under a run_id namespace that does not match the existing rows,
# so they would read as unsatisfied forever. Same retry semantics as
# run_pool_vs_pi.sh, which closed 14 of 15 cells.
set -uo pipefail
cd ~/dev/openbench
set -a
source ~/.openbench/keys.env 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"
export POOLSIDE_API_KEY="${OPENROUTER_API_KEY:-}"
export POOLSIDE_STANDALONE_BASE_URL="https://openrouter.ai/api/v1"
set +a
PACK=".openbench/packs/openbench/tb-mid/1.0.0"
OUT=results/pool-refill-n3.jsonl
L=results/pool-refill-ledger
mkdir -p $L

# task:trial pairs, not a task list -- only overfull-hbox trial 1 is missing.
CELLS="overfull-hbox:1 winning-avg-corewars:1 winning-avg-corewars:2 winning-avg-corewars:3"

for cell in $CELLS; do
  T="${cell%%:*}"; tr="${cell##*:}"
  for attempt in 1 2 3 4; do
    OPENBENCH_PROXY_LEDGER_DIR=$L python3 -m obench.run --force \
      --candidate experiments/candidates/pool.toml --model laguna-s-2.1 \
      --task "$T" --tasks-dir "$PACK" --trial "$tr" --trials 3 --timeout 2400 \
      --proxy --exec docker --results-path "$OUT" >/dev/null 2>&1
    if python3 - "$OUT" "$T" "$tr" <<'PY'
import json,sys
p,t,tr=sys.argv[1:4]
ok=False
for l in open(p):
    r=json.loads(l)
    if r.get("task")==t and str(r.get("trial"))==tr and \
       r.get("failure_class") not in ("infra","rate_limited","stalled"):
        ok=True
sys.exit(0 if ok else 1)
PY
    then echo "OK   pool/$T/t$tr (attempt $attempt)"; break; fi
    echo "RETRY pool/$T/t$tr attempt $attempt"; sleep $((attempt*300))
  done
done
touch results/poolrefill.DONE
echo "=== pool refill done"
