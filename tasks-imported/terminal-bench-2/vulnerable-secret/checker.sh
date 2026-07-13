#!/usr/bin/env bash
set -euo pipefail
TASK_DIR="${TASK_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
WORKDIR="/app"
IMAGE="openbench-tb2-vulnerable-secret:pinned"
PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tb2-vulnerable-secret-logs.XXXXXX")"
CIDFILE="$(mktemp "${TMPDIR:-/tmp}/tb2-vulnerable-secret-cid.XXXXXX")"; rm -f "$CIDFILE"
cleanup() { if [ -s "$CIDFILE" ]; then docker rm -f "$(cat "$CIDFILE")" >/dev/null 2>&1 || true; fi; rm -rf "$LOG_DIR" "$CIDFILE"; }
trap cleanup EXIT; mkdir -p "$LOG_DIR"
set +e
docker run --rm --network none --platform "$PLATFORM" --cidfile "$CIDFILE" -v "$PWD:$WORKDIR" -v "$TASK_DIR/checker_data/tests:/tests:ro" -v "$LOG_DIR:/logs/verifier" -w "$WORKDIR" "$IMAGE" bash /tests/test.sh
container_status=$?; set -e
reward_file="$LOG_DIR/reward.txt"
if [ ! -s "$reward_file" ]; then echo "SCORE: 0"; echo "FAIL: verifier did not produce /logs/verifier/reward.txt (container exit $container_status)"; exit 1; fi
reward="$(tr -d '[:space:]' < "$reward_file")"
python3 - "$reward" <<'INNER_PY'
import math,sys
try: v=float(sys.argv[1])
except Exception: v=0.0
if not math.isfinite(v): v=0.0
v=max(0.0,min(1.0,v)); print(f"SCORE: {v:g}"); sys.exit(0 if v>=1.0 else 1)
INNER_PY
