#!/usr/bin/env bash
# Final completion chain: wait for pi recovery -> purge feal-137 junk ->
# bump VM to 12GB -> retry pi deepseek feal -> grok kimi column.
set -u
cd ~/dev/openbench
source ~/.openbench/keys.env
mkdir -p .bench-tmp logs
export TMPDIR=$PWD/.bench-tmp
TASKS=terminal-bench/cancel-async-tasks,terminal-bench/feal-differential-cryptanalysis,terminal-bench/llm-inference-batching-scheduler,terminal-bench/schemelike-metacircular-eval

while pgrep -f recovery_pi.sh >/dev/null; do sleep 60; done
echo "=== pi recovery drained ==="

python3 - <<PYEOF
import json, shutil
p=results/tb-open-n3-deepseek-v4-flash.jsonl
shutil.copy2(p, p+.pre-fealretry.bak)
rows=[json.loads(l) for l in open(p)]
keep=[r for r in rows if not (r[harness]==pi and feal in r[task] and 137 in str(r.get(error) or ))]
with open(p,w) as fh:
    for r in keep: fh.write(json.dumps(r)+n)
print(purged, len(rows)-len(keep), feal-137 rows)
PYEOF

echo "=== bumping VM to 12GB ==="
/opt/homebrew/opt/colima/bin/colima stop
/opt/homebrew/opt/colima/bin/colima start --memory 12 --cpus 4

echo "=== pi deepseek feal retry at 12GB ==="
python3 bench/run.py --task terminal-bench/feal-differential-cryptanalysis --harness pi --model deepseek-v4-flash \
  --trials 3 --tasks-dir tasks-imported --exec docker --no-docker-fallback \
  --timeout 1200 --checker-timeout 300 \
  --results-path results/tb-open-n3-deepseek-v4-flash.jsonl

echo "=== grok kimi column ==="
python3 bench/run.py --task "$TASKS" --harness grokbuild --model kimi-k2.7-code \
  --trials 3 --tasks-dir tasks-imported --exec docker --no-docker-fallback \
  --timeout 1200 --checker-timeout 300 \
  --results-path results/tb-open-n3-kimi-k2.7-code.jsonl
echo "=== final chain complete ==="
