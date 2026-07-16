# Multi-agent toggle experiment

## Question and fixed design

Measure the token and latency cost of enabling harness-native subagents, with
special attention to context prefixes repeated across child sessions. Use the
same 15 admission-gated tasks, five trials, GPT-5.6 Sol (`gpt-5.6-sol`) through
the Codex subscription, and the counting proxy in both arms. Do not mix these
rows with API-key or open-model runs.

The current pair is:

| arm | runner expression | only intended difference |
|---|---|---|
| Codex OFF | stock `--harness codex` | adapter passes `--disable multi_agent` |
| Codex ON | `--candidate experiments/multiagent-toggle/codex-on.toml` | adapter passes `--enable multi_agent` |

The candidate still uses the stock adapter's model, effort, service-tier,
sandbox, config isolation, auth, parser, apps-off, and plugins-off pins. Its
staged `config.toml` contains no behavioral settings.

A matched grok-build pair using `GROK_SUBAGENTS` is **pending** until the
`grokbuild-bench` branch merges. This experiment neither imports nor depends on
that branch.

## Tasks and sample size

The 15-task set is the prior gated GPT-5.6 Sol set:

- core (8): `add-feature`, `build-a-cli`, `fix-failing-test`, `make-ci-green`,
  `make-it-run`, `misleading-error`, `taskflow`, `webcore`
- imported Terminal-Bench (7): `db-wal-recovery`, `extract-elf`,
  `feal-differential-cryptanalysis`, `gcode-to-text`,
  `llm-inference-batching-scheduler`, `raman-fitting`,
  `schemelike-metacircular-eval`

Run `n=5` per arm/task: 150 cells total. The four imported batch-1 gate records
are under `data/admission-gate-import-batch1/`; the other records are summarized
in `data/admission-gate-report-2026-07-11.md`. Do not substitute the dropped
`cancel-async-tasks` task.

## Mac mini commands

Run from the Mac mini checkout root. These commands deliberately keep arm
results and per-cell ledgers separate. They are live, token-spending commands;
do **not** run them during code review.

```bash
cd ~/dev/openbench
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ROOT="$PWD/results/multiagent-toggle-$STAMP"
mkdir -p "$ROOT"/{ledger/off-core,ledger/off-tb,ledger/on-core,ledger/on-tb,prefix}

CORE='add-feature,build-a-cli,fix-failing-test,make-ci-green,make-it-run,misleading-error,taskflow,webcore'
TB='terminal-bench/db-wal-recovery,terminal-bench/extract-elf,terminal-bench/feal-differential-cryptanalysis,terminal-bench/gcode-to-text,terminal-bench/llm-inference-batching-scheduler,terminal-bench/raman-fitting,terminal-bench/schemelike-metacircular-eval'

# OFF, stock adapter (40 core + 35 imported cells)
OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger/off-core" python3 bench/run.py \
  --harness codex --model gpt-5.6-sol --task "$CORE" --trials 5 \
  --timeout 1200 --proxy --results-path "$ROOT/codex-off.jsonl"
OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger/off-tb" python3 bench/run.py \
  --harness codex --model gpt-5.6-sol --task "$TB" --tasks-dir tasks-imported \
  --trials 5 --timeout 1200 --proxy --results-path "$ROOT/codex-off.jsonl"

# ON, declarative candidate (40 core + 35 imported cells)
OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger/on-core" python3 bench/run.py \
  --candidate experiments/multiagent-toggle/codex-on.toml \
  --model gpt-5.6-sol --task "$CORE" --trials 5 --timeout 1200 --proxy \
  --results-path "$ROOT/codex-on.jsonl"
OPENBENCH_PROXY_LEDGER_DIR="$ROOT/ledger/on-tb" python3 bench/run.py \
  --candidate experiments/multiagent-toggle/codex-on.toml \
  --model gpt-5.6-sol --task "$TB" --tasks-dir tasks-imported \
  --trials 5 --timeout 1200 --proxy --results-path "$ROOT/codex-on.jsonl"

# Canonical solve/hack-adjusted and efficiency summaries.
python3 bench/stats.py --strict-provenance --min-n 75 \
  --tasks-dir tasks --tasks-dir tasks-imported \
  "$ROOT/codex-off.jsonl" "$ROOT/codex-on.jsonl" | tee "$ROOT/stats.txt"
python3 bench/report.py --efficiency --results-path "$ROOT/codex-off.jsonl" \
  | tee "$ROOT/off-efficiency.txt"
python3 bench/report.py --efficiency --results-path "$ROOT/codex-on.jsonl" \
  | tee "$ROOT/on-efficiency.txt"

# One analysis object per cell ledger; each output file preserves the cell token.
for arm in off-core off-tb on-core on-tb; do
  for ledger in "$ROOT/ledger/$arm"/*.jsonl; do
    cell=$(basename "$ledger" .jsonl)
    python3 bench/analyze_prefix.py "$ledger" > "$ROOT/prefix/$arm-$cell.json"
  done
done
```

Both runs are resumable. Keep the candidate file unchanged after starting: its
content digest is part of ON run IDs.

## Metrics and interpretation

Report by arm and paired task/trial where possible:

- solve rate and hack-adjusted solve rate/score from `bench/stats.py`
- wall time per solve
- proxy uncached-input, cache-read, cache-write, and output tokens
- observed session/conversation component count
- duplicated-prefix token estimate

`bench/analyze_prefix.py` totals all input lanes. It links requests using
per-proxy-run salted hashes of explicit session IDs or response /
previous-response IDs. For each later session, it estimates duplicated prefix as
`min(first request input in that session, first request input in the first
session)`.

This is a size-overlap estimate, not content matching: the ledger intentionally
stores no prompts. Equal token counts can represent different text, and cache
reads prove provider-side reuse but not which session supplied it. If the wire
protocol exposes no linkable IDs, the analyzer emits null session and
duplication values rather than inventing a count. Truncated proxy captures can
also hide a response ID; retain `unidentified_calls` in analysis tables.
