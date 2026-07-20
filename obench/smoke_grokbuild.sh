#!/usr/bin/env bash
# One-cell, one-open-model Grok Build smoke for an authenticated bench host.
# This spends vendor tokens. It is an operator entry point, not a unit test.
set -euo pipefail

: "${DEEPSEEK_API_KEY:?load DEEPSEEK_API_KEY (for example from ~/.openbench/keys.env)}"

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

exec python3 bench/run.py \
  --harness grokbuild \
  --model deepseek-v4-flash \
  --task make-it-run \
  --trials 1 \
  --proxy \
  "$@"
