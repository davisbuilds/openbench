# TB open-model n=3 — 2026-07-09

Committed, scrubbed publication snapshot of the Terminal-Bench open-model matrix.

## Dataset shape

- **Rows:** 180 JSONL rows across three files (60 rows/model).
- **Panel:** 5 harnesses × 3 models × 4 Terminal-Bench tasks × 3 trials.
- **Harnesses:** `pi`, `opencode`, `claude`, `codex`, `grokbuild`.
- **Models:** `deepseek-v4-flash`, `glm-5.2`, `kimi-k2.7-code`.
- **Tasks:** `cancel-async-tasks`, `feal-differential-cryptanalysis`, `llm-inference-batching-scheduler`, `schemelike-metacircular-eval`.
- **Collection window:** 2026-07-07 through 2026-07-08 local run timestamps; promoted on 2026-07-09.

The JSONL files are scrubbed copies of local `results/tb-open-n3-*.jsonl` files.
No transcripts are included; full-output/transcript-style fields were not present in
these promoted rows.

## Included artifacts

- `tb-open-n3-deepseek-v4-flash.jsonl`
- `tb-open-n3-glm-5.2.jsonl`
- `tb-open-n3-kimi-k2.7-code.jsonl`
- `tb-open-n3-stats.md`
- `tb-open-n3-methodology-notes.md`
- `charts/` (`index.html` plus PNG charts)

## Verification chain summary

This snapshot follows the promotion chain documented in the methodology notes:
write-time classifier, contamination purge and rerun, independent audit, parity
backfill, and adversarial review. The promoted files were additionally checked for
API keys, bearer/auth headers, provider key variable names, email addresses,
home-path secret leaks, and long base64/hex-like runs before commit.

See [`tb-open-n3-methodology-notes.md`](tb-open-n3-methodology-notes.md) for the
full methodology, audit notes, denominator choices, timeout handling, and telemetry
caveats. Summary statistics and tables are in
[`tb-open-n3-stats.md`](tb-open-n3-stats.md).
