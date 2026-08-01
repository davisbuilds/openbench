#!/usr/bin/env bash
set -uo pipefail
cd ~/dev/openbench
while [ ! -f results/phase2.DONE ]; do sleep 120; done
set -a
source ~/.openbench/keys.env 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"
set +a
python3 -m obench.run --harness pi --model gpt-5.6-sol --task adaptive-rejection-sampler,query-optimize,winning-avg-corewars,merge-diff-arc-agi-task,overfull-hbox,sanitize-git-repo --tasks-dir .openbench/packs/openbench/tb-mid/1.0.0 --trials 3 --timeout 2400 --proxy --exec docker --results-path results/tb-mid-baseline-n3.jsonl || true
touch results/gptfill.DONE
