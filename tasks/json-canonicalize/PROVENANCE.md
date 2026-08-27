# Provenance

- Source: authored locally for OpenBench (original task, not imported).
- Author: fork-local (harder graded task set, 2026-08).
- Added: 2026-08-27 (discriminating task-set expansion — graded partial credit).
- Oracle: solution/canon.py; checker_data/*.expected are generated FROM that
  oracle (see the corpus builder in the task history), so oracle and fixtures
  are self-consistent by construction.
- Scoring: GRADED. checker.sh emits `SCORE: passed/total` and exits nonzero on
  any miss, so partial credit is recorded; exit 0 (full pass) forces score 1.0.
