# Inspecting result files

`obench results summary`, `pertask`, `matched`, and `errors` validate the input
identity before aggregating. Harbor inputs use their embedded comparison plans;
sealed suites must retain their complete intended denominator. Filters apply
only after that validation, so a model filter cannot hide a broken suite.

Malformed JSON, non-object rows, duplicate JSON keys and nonfinite JSON constants
fail with the source file and line. `obench report` uses the same strict loader.
Blank lines are allowed. A report never silently discards a damaged row.

```sh
obench results summary results/study.jsonl
obench results matched results/study.jsonl
obench results summary results/old.jsonl results/new.jsonl --separate-inputs
```

The last command produces independent reports. It does not pool treatments or
claim matched trials across studies. Harbor profiles retain their arm IDs even
when their harness/model labels are identical. Missing planned attempts reduce
Harbor coverage; legacy coverage uses observed cells and cannot prove that the
entire intended campaign ran.

Legacy rows retain judged-before-excluded, then latest-timestamp retry selection
within one consistent cell identity. Conflicting study IDs, task digests or run
IDs fail instead of silently replacing an attempt. Historical rows without these
fields do not establish treatment equivalence; use separately identified inputs
when that provenance is unknown. No historical data is rewritten.

`obench results evidence FILE --run-id SUBSTRING` remains available to inspect
valid JSON from mixed treatments without calculating aggregate statistics.
