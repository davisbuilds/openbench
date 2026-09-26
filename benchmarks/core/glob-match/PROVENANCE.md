# Provenance

- Source: authored locally for OpenBench (original task, not imported).
- Author: fork-local (harder graded task set, 2026-08).
- Added: 2026-08-27 (discriminating task-set expansion — graded partial credit).
- Oracle: solution/glob_match.py; checker_data/cases.tsv expected verdicts are
  generated FROM that oracle, so oracle and fixtures are self-consistent.
- Scoring: GRADED. checker.sh emits `SCORE: passed/total` and exits nonzero on
  any miss so partial credit is recorded; exit 0 (full pass) forces score 1.0.
- Note: `**` semantics are defined literally (a run of 2+ '*' matches any chars
  incl '/', with no zero-segment collapse). The instruction states this exactly;
  the task tests spec-adherence to THAT definition, not a particular ecosystem's
  globstar.
