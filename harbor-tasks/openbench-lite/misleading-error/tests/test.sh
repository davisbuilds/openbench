#!/usr/bin/env bash
# OpenBench → Harbor verifier: run checker.sh, map exit/SCORE → reward.txt.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export TASK_DIR="$TESTS_DIR"
export PYTHONDONTWRITEBYTECODE=1

# Prefer Harbor's /logs/verifier when present; else VERIFIER_LOGS_DIR or
# ./logs-verifier for local round-trip harnesses without a /logs mount.
if [ -n "${VERIFIER_LOGS_DIR:-}" ]; then
  REWARD_DIR="$VERIFIER_LOGS_DIR"
elif [ -d /logs/verifier ]; then
  REWARD_DIR="/logs/verifier"
else
  REWARD_DIR="$(pwd)/logs-verifier"
fi
mkdir -p "$REWARD_DIR"

START_EPOCH="$(date +%s 2>/dev/null || true)"
OUT_FILE="$(mktemp)"
set +e
bash "$TESTS_DIR/checker.sh" >"$OUT_FILE" 2>&1
RC=$?
set -e
cat "$OUT_FILE"

PARSED_SCORE="$(
  awk '
    /^[[:space:]]*SCORE:[[:space:]]*/ {
      line=$0
      sub(/^[[:space:]]*SCORE:[[:space:]]*/, "", line)
      split(line, a, /[[:space:]]+/)
      candidate=a[1]
      if (candidate ~ /^[-+]?(([0-9]+([.][0-9]*)?)|([.][0-9]+))([eE][-+]?[0-9]+)?$/) {
        value=candidate + 0
        if (value < 0) value=0
        if (value > 1) value=1
        last=sprintf("%.17g", value)
      }
    }
    END { if (last != "") print last }
  ' "$OUT_FILE"
)"

if [ "$RC" -eq 0 ]; then
  REWARD="1.0"
elif [ -n "$PARSED_SCORE" ]; then
  REWARD="$PARSED_SCORE"
else
  REWARD="0.0"
fi

printf '%s\n' "$REWARD" >"$REWARD_DIR/reward.txt"
END_EPOCH="$(date +%s 2>/dev/null || true)"
case "$START_EPOCH:$END_EPOCH" in
  :*|*:|*[!0-9:]*) DURATION_JSON="null" ;;
  *)
    if [ "$END_EPOCH" -ge "$START_EPOCH" ]; then
      DURATION_JSON="$((END_EPOCH - START_EPOCH))"
    else
      DURATION_JSON="null"
    fi
    ;;
esac
if [ -n "$PARSED_SCORE" ]; then
  PARSED_SCORE_JSON="$PARSED_SCORE"
else
  PARSED_SCORE_JSON="null"
fi
cat >"$REWARD_DIR/openbench-verifier-evidence.json" <<EOF
{
  "schema_version": "openbench-verifier-evidence-v2",
  "openbench_task_content_digest": {
    "scheme": 2,
    "sha256": "0049b22b033d1df2c229d0f24ab681bb3ed3f450cb7f9aaa6c6c01899401290e"
  },
  "openbench_harbor_export": {
    "schema_version": 1,
    "base_image": "python:3.11-slim",
    "network_mode": "no-network"
  },
  "checker_exit": $RC,
  "parsed_score": $PARSED_SCORE_JSON,
  "reward": $REWARD,
  "verifier_duration_seconds": $DURATION_JSON
}
EOF
rm -f "$OUT_FILE"
exit 0
