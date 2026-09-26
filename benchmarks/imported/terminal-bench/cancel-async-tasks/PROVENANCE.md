# Provenance

- **Upstream project**: Terminal-Bench (https://github.com/laude-institute/terminal-bench)
- **Upstream task**: `cancel-async-tasks` (original-tasks/cancel-async-tasks)
- **Upstream commit**: `1a6ffa9674b571da0ed040c470cb40c4d85f9b9b`
- **License**: Apache-2.0 (see the repository LICENSE; the upstream task's canary
  string is intentionally omitted from the imported files)
- **Upstream difficulty**: `hard`

## Modifications made during conversion

- `instruction.md`: upstream `instruction` prose, adapted so paths are relative
  to the working directory (`run.py`) instead of `/app/run.py`.
- `workspace/`: empty starting state (the upstream Dockerfile copies nothing);
  a `.gitkeep` placeholder keeps the directory tracked.
- `checker.sh` + `checker_data/run_checks.py`: a pure-stdlib re-implementation of
  the upstream `tests/test_outputs.py` (6 concurrency/cancellation scenarios),
  driving the upstream `tests/test.py` harness (copied verbatim to
  `checker_data/test.py`). pytest is not used, so the checker runs on the minimal
  openbench-harness image. `python` invocations were changed to `python3`.
- `solution/run.py`: the reference implementation extracted verbatim from the
  upstream `solution.sh`.
- `checker_data/run_checks.py`: cancellation checks were hardened to wait for
  readiness from unbuffered child output before sending SIGINT, with documented
  timing slack for host load.
- `instruction.md`: clarified that cancellation must not start queued tasks and
  only already-started tasks require cleanup.
