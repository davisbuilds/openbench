#!/usr/bin/env bash
# Close the 11 coverage gaps. --force is required: each cell already has an
# excluded row, which the plain runner treats as "done" without it.
set -uo pipefail
cd ~/dev/openbench
set -a
source ~/.openbench/keys.env 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"
set +a
R=results/laguna-inkling-20260721T193000Z
OUT=$R/laguna-inkling.jsonl
cell() { # harness model task trial [docker]
  local h=$1 m=$2 t=$3 tr=$4 ex=${5:-local}
  for attempt in 1 2 3 4 5 6; do
    local args=(--force --harness "$h" --model "$m" --task "$t" --trial "$tr" --trials 3 --timeout 1200 --proxy --results-path "$OUT")
    [ "$ex" = docker ] && args+=(--tasks-dir tasks-imported --exec docker)
    OPENBENCH_PROXY_LEDGER_DIR=$R/ledger python3 -m obench.run "${args[@]}" >/dev/null 2>&1
    if python3 - "$OUT" "$h" "$m" "$t" "$tr" <<PY
import json,sys
p,h,m,t,tr=sys.argv[1:6]
ok=any(json.loads(l).get("failure_class") not in ("infra","rate_limited","stalled")
       for l in open(p)
       if (lambda r: r["harness"]==h and r["model"]==m and r["task"]==t and str(r["trial"])==tr)(json.loads(l)))
sys.exit(0 if ok else 1)
PY
    then echo "OK   $h/$m/$t/t$tr (attempt $attempt)"; return 0; fi
    echo "RETRY $h/$m/$t/t$tr attempt $attempt"; sleep $((attempt*120))
  done
  echo "GAVE UP $h/$m/$t/t$tr"
}
for tr in 1 2 3; do cell pi laguna-s-2.1 webcore $tr; done
cell pi laguna-s-2.1 terminal-bench/extract-elf 3 docker
for tr in 1 2 3; do cell opencode laguna-s-2.1 webcore $tr; done
for tr in 2 3; do cell pi inkling webcore $tr; done
cell pi inkling terminal-bench/db-wal-recovery 1 docker
cell pi inkling terminal-bench/schemelike-metacircular-eval 1 docker
touch results/gapfill.DONE
