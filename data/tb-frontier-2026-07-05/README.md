# Terminal-Bench frontier run — docker lane (run 2026-07-05)

Committed snapshot of the Terminal-Bench frontier dataset: five imported
Terminal-Bench tasks run against the three container-compatible frontier
harnesses. `results.jsonl` is the scrubbed raw log for the frontier matrix (45
rows); `shakeout.jsonl` is the scrubbed one-trial DeepSeek baseline (5 rows).
Run window: 2026-07-05 local, single macOS host.

## What was run

- **Matrix:** 3 harnesses (`pi`, `codex`, `opencode`) × 5
  `terminal-bench/*` tasks × **3 trials** = 45 frontier cells.
- **Model:** canonical `gpt-5.5-medium`, docker execution lane, in the
  `openbench-harness:latest` image.
- **Shakeout baseline:** `deepseek-v4-flash` on the same 5 tasks × **1 trial**
  (`shakeout.jsonl`).
- **Convention-independent snapshot:** this README records correctness and
  provenance only. Convention-dependent wall-time/token summaries belong in
  `RESULTS.md`.

## Per-task solve matrix

Verified from `data/tb-frontier-2026-07-05/results.jsonl` (`success == true`):

| Task | pi | codex | opencode |
|------|----|-------|----------|
| `cancel-async-tasks` | 3/3 | 3/3 | 3/3 |
| `count-call-stack` | 0/3 | 0/3 | 0/3 |
| `feal-differential-cryptanalysis` | 3/3 | 3/3 | 3/3 |
| `llm-inference-batching-scheduler` | 3/3 | 3/3 | 3/3 |
| `schemelike-metacircular-eval` | 3/3 | 3/3 | 3/3 |
| **Total** | **12/15** | **12/15** | **12/15** |

## Caveats

- **`count-call-stack` was a universal miss:** 0/9 frontier trials and 0/1
  `deepseek-v4-flash` shakeout trial. It is an exact-match precision task, so
  small formatting/counting mistakes are scored as failures.
- **`opencode` `schemelike-metacircular-eval` trial 1 hit the timeout ceiling:**
  the checker passed, so it is counted as a solve, but the row ended at the
  1800s cap with `tokens = null`.
- **`cursor` and `devin` are excluded from this Terminal-Bench snapshot:**
  cursor keychain auth cannot be containerized for this docker lane, and devin
  was flaky/free-plan rather than a stable comparable runner.
- **Home paths are scrubbed:** committed JSONL copies replace local home paths
  with `<HOME>`.

## Provenance

- `results.jsonl` is a scrubbed copy of `results/tb-frontier.jsonl`.
- `shakeout.jsonl` is a scrubbed copy of `results/tb-shakeout.jsonl`.
- Tasks are imported under `tasks-imported/terminal-bench` from upstream
  [Terminal-Bench](https://github.com/laude-institute/terminal-bench), licensed
  Apache-2.0. See `tasks-imported/terminal-bench/README.md` and each task's
  `PROVENANCE.md` for conversion notes.

## Reproduce

```
python3 bench/run.py --task terminal-bench/<name> --harness <h> --exec docker \
    --tasks-dir tasks-imported
```
