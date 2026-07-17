# Comparing benchmark arms

Use `bench/compare.py` to create a report-ready scorecard from two or more
OpenBench result arms:

```sh
python3 bench/compare.py results/baseline.jsonl results/candidate.jsonl \
  --markdown results/scorecard.md
```

The command always prints a human-readable table. `--markdown PATH` also writes
a Markdown table with arms as columns and metrics as rows.

## Arms and matching

With multiple input paths, each file is one arm and its filename stem is the
column label. With one input path, rows are split by
`candidate_provenance.name`, falling back to `harness`; this supports combined
runs containing baseline and candidate or multiple harness arms.

The headline denominator is the intersection of unique `(task, trial)` cells
present in every arm after exclusions. Extra cells do not affect any arm's
headline metrics. Duplicate cells within an arm are ambiguous, so they are
removed from matching and reported. The scorecard states the matched `n` before
the table and reports unmatched rows per arm.

Rows classified as `infra` or `rate_limited` are excluded before matching and
from solve-rate denominators. Their counts are shown separately for every arm,
along with invalid-row and dropped-task quarantine counts. Invalid rows that
cannot be attributed to an arm are reported as unassigned exclusions. Canonical filtering,
Wilson intervals, hack-adjusted scoring, and provenance checks are reused from
`bench/stats.py`. A prominent warning identifies provenance differences; add
`--strict-provenance` to exit with status 2 when they occur.

## Metrics

The scorecard contains:

- solve count/rate and Wilson 95% confidence interval;
- hack-adjusted rate (mean canonical `score`, with legacy success fallback);
- total wall time divided by solves;
- uncached-input, cache-read, and output tokens divided by solves; and
- harness versions observed in all countable rows for each arm.

A version cell is marked `[MIXED]` when an arm contains multiple
`harness_version` values, including in unmatched countable rows. Missing efficiency measurements render as `-` rather
than being counted as zero; a per-solve metric is unavailable if any matched
row lacks that measurement.

Use `--tasks-dir PATH` (repeatable or comma-separated) when result task roots
differ from the repository defaults. Tasks containing `DROPPED.md` are excluded
using the same canonical filtering as `bench/stats.py`.
