# Provenance

- **Upstream project**: Terminal-Bench (https://github.com/laude-institute/terminal-bench)
- **Upstream task**: `count-call-stack` (original-tasks/count-call-stack)
- **Upstream commit**: `1a6ffa9674b571da0ed040c470cb40c4d85f9b9b`
- **License**: Apache-2.0 (see the repository LICENSE)
- **Upstream difficulty**: `easy` (included as a deterministic, low-variance
  anchor; the exact output-format requirements still trip up many agents)

## Modifications made during conversion

- `instruction.md`: upstream `instruction` prose, adapted so the log path
  (`log.stack`) and output path (`output.txt`) are relative to the working
  directory instead of `/app/...`.
- `workspace/log.stack`: extracted from the upstream `log.stack.zip` (the minimal
  image has no `unzip`), shipped uncompressed (~4 MB).
- `checker.sh` + `checker_data/run_checks.py`: a pure-stdlib re-implementation of
  the upstream `tests/test_outputs.py`, comparing `output.txt` to the upstream
  `expected_output.txt` line-by-line with trailing whitespace stripped (identical
  semantics to the upstream pytest assertion).
- `checker_data/expected_output.txt`: the upstream reference output, verbatim.
- `solution/output.txt`: the upstream reference output (identical to
  `expected_output.txt`); this is the authoritative solved deliverable.
