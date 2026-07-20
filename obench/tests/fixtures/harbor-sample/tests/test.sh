#!/usr/bin/env bash
# Synthetic Harbor verifier: reward.json with primary "reward" field.
set -uo pipefail

REWARD_DIR="${VERIFIER_LOGS_DIR:-/logs/verifier}"
mkdir -p "$REWARD_DIR"

got="$(cat greeting.txt 2>/dev/null || true)"
if [ "$got" = "hello" ]; then
  printf '%s\n' '{"reward": 1.0, "accuracy": 1.0}' >"$REWARD_DIR/reward.json"
else
  printf '%s\n' '{"reward": 0.0, "accuracy": 0.0}' >"$REWARD_DIR/reward.json"
fi
exit 0
