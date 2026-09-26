#!/usr/bin/env bash
# pool x laguna over the wide25 task set: 25 tasks x 2 trials = 50 cells.
# The co-tuning test: pi x laguna measured 6% on this exact set; Poolside's own
# harness beat pi on tb-mid (44%-floor vs 28%) under storm contamination, and
# their published TB-2.1 number (70.2) implies a strong harness. This run
# arbitrates with clean conditions: proxy pacing applies automatically (pool's
# traffic routes through the counting proxy), and per-cell 429 health comes
# from the proxy ledger (pool is a BYO candidate, so the pi reply-health fields
# do not apply; the ledger sweep is the proven substitute).
set -uo pipefail
cd ~/dev/openbench
set -a; source ~/.openbench/keys.env 2>/dev/null; set +a
: "${POOLSIDE_API_KEY:?POOLSIDE_API_KEY missing from keys.env}"
OUT=results/pool-wide25/pool.jsonl
L=results/pool-wide25/ledger
mkdir -p results/pool-wide25 "$L"

cell_satisfied() { # task trial
  python3 - "$OUT" "$1" "$2" <<'PY2' 2>/dev/null
import json,sys,os
p,t,tr=sys.argv[1:4]
if not os.path.isfile(p): sys.exit(1)
for l in open(p):
    r=json.loads(l)
    if r.get("task")==t and str(r.get("trial"))==tr:
        fc=r.get("failure_class")
        if fc in ("infra","rate_limited","stalled"): continue
        if "verifier did not produce" in (r.get("checker_stdout") or ""): continue
        if fc=="timeout" and (r.get("timeout_s") or 0) < 7200: continue
        sys.exit(0)
sys.exit(1)
PY2
}

wait_for_weather() {
  # 92% of requests 429'd in the window that killed the first launch. Two
  # spaced probes must succeed before burning full cells; probes cost ~cents.
  while true; do
    good=0
    for i in 1 2; do
      code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30         https://inference.poolside.ai/v1/chat/completions         -H "Authorization: Bearer $POOLSIDE_API_KEY" -H "Content-Type: application/json"         -d '{"model":"poolside/laguna-s-2.1","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}')
      [ "$code" = "200" ] && good=$((good+1))
      sleep 8
    done
    [ "$good" = "2" ] && { echo "weather OK ($(date +%H:%M))"; return; }
    echo "still stormy ($(date +%H:%M), $good/2 probes ok); re-probing in 5m"
    sleep 300
  done
}

run_cell() { # dir task trial
  local dir=$1 T=$2 tr=$3
  if cell_satisfied "$T" "$tr"; then echo "SKIP $T/t$tr (already satisfied)"; return 0; fi
  wait_for_weather
  for attempt in 1 2 3 4; do
    OPENBENCH_PROXY_LEDGER_DIR=$L python3 -m obench.run --force \
      --candidate experiments/candidates/pool.toml --model laguna-s-2.1 \
      --task "$T" --tasks-dir "$dir" --trial "$tr" --trials 2 --timeout 7200 \
      --proxy --exec docker --results-path "$OUT" >/dev/null 2>&1
    if python3 - "$OUT" "$T" "$tr" <<'PY'
import json,sys
p,t,tr=sys.argv[1:4]
ok=False
for l in open(p):
    r=json.loads(l)
    if r.get("task")==t and str(r.get("trial"))==tr:
        fc=r.get("failure_class")
        if fc in ("infra","rate_limited","stalled"): continue
        if "verifier did not produce" in (r.get("checker_stdout") or ""): continue
        if fc=="timeout" and (r.get("timeout_s") or 0) < 7200: continue
        ok=True
sys.exit(0 if ok else 1)
PY
    then echo "OK   pool/$T/t$tr (attempt $attempt)"; return 0; fi
    echo "RETRY pool/$T/t$tr attempt $attempt"; sleep $((attempt*120))
  done
  echo "GAVEUP pool/$T/t$tr"
}

preflight_images() {
  python3 - "$TBMID_DIR" "$TB2_DIR" "$TB1_DIR" <<'PYIMG'
import json, os, re, subprocess, sys
missing = []
def have(ref):
    return subprocess.run(["docker", "image", "inspect", ref],
                          capture_output=True).returncode == 0
for d in sys.argv[1:4]:
    imgmap = {}
    ij = os.path.join(d, "images.json")
    if os.path.isfile(ij):
        imgmap = json.load(open(ij))
    for t in sorted(os.listdir(d)):
        ck = os.path.join(d, t, "checker.sh")
        if not os.path.isfile(ck):
            continue
        src = open(ck).read()
        m = re.search(r'^IMAGE="([^"]+)"', src, re.M)
        if not m:
            continue  # checker does not use docker
        if "BENCH_TASK_IMAGE" in m.group(1):
            # production supplies BENCH_TASK_IMAGE from the pack images.json
            ref = imgmap.get(t, {}).get("tag") or imgmap.get(t, {}).get("digest")
            if not ref:
                missing.append(f"{t}: no images.json entry for BENCH_TASK_IMAGE checker")
                continue
        else:
            ref = m.group(1)
        if not have(ref):
            missing.append(f"{t}: {ref}")
for line in missing:
    print("MISSING checker image:", line)
sys.exit(3 if missing else 0)
PYIMG
  [ $? -eq 0 ] || { echo "preflight failed: build missing checker images first"; exit 3; }
  echo "preflight: all checker images present"
}

TBMID_DIR=data/packs/openbench-tb-mid
TB2_DIR=tasks-imported/terminal-bench-2
TB1_DIR=tasks-imported/terminal-bench
TBMID="adaptive-rejection-sampler merge-diff-arc-agi-task overfull-hbox query-optimize sanitize-git-repo winning-avg-corewars"
TB2="cobol-modernization constraints-scheduling dna-assembly extract-elf log-summary-date-ranges path-tracing polyglot-c-py prove-plus-comm regex-log sparql-university sqlite-db-truncate vulnerable-secret"
TB1="count-call-stack db-wal-recovery feal-differential-cryptanalysis gcode-to-text llm-inference-batching-scheduler raman-fitting schemelike-metacircular-eval"

preflight_images

# Outer sweep: a GAVEUP abandons its cell for the pass, so a storm that starts
# mid-cell would otherwise leave holes that only a manual relaunch fills. Sweep
# until every cell is satisfied or a full pass fixes nothing (hard-stuck).
for sweep in 1 2 3; do
  echo "=== sweep $sweep"
  before=$(grep -c "^OK" /tmp/pool-wide25.log 2>/dev/null || echo 0)
  for tr in 1 2; do
    for T in $TBMID; do run_cell "$TBMID_DIR" "$T" "$tr"; done
    for T in $TB2;   do run_cell "$TB2_DIR" "$T" "$tr"; done
    for T in $TB1;   do run_cell "$TB1_DIR" "$T" "$tr"; done
  done
  remaining=0
  for tr in 1 2; do
    for T in $TBMID $TB2 $TB1; do
      cell_satisfied "$T" "$tr" || remaining=$((remaining+1))
    done
  done
  echo "=== sweep $sweep done; unsatisfied cells: $remaining"
  [ "$remaining" = "0" ] && break
  after=$(grep -c "^OK" /tmp/pool-wide25.log 2>/dev/null || echo 0)
  [ "$after" = "$before" ] && { echo "=== no progress this sweep; stopping"; break; }
done
touch results/pool-wide25/DONE
echo "=== pool wide25 complete"
