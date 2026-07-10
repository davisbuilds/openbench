# Provenance

- **Upstream project**: Terminal-Bench (https://github.com/laude-institute/terminal-bench)
- **Upstream task**: `schemelike-metacircular-eval` (original-tasks/schemelike-metacircular-eval)
- **Upstream commit**: `1a6ffa9674b571da0ed040c470cb40c4d85f9b9b`
- **License**: Apache-2.0 (see the repository LICENSE)
- **Upstream difficulty**: `medium` (writing a self-hosting metacircular
  evaluator is empirically very hard for frontier agents)

## Modifications made during conversion

- `instruction.md`: upstream `instruction` prose, adapted so paths are relative
  to the working directory instead of `/app/...`.
- `workspace/interp.py`: the reference interpreter, copied verbatim.
- `workspace/test/` and `workspace/shadow_test/`: the upstream test programs,
  copied verbatim from `tests/test/` and `tests/shadow_test/`. (Upstream keeps a
  held-out `shadow_test/` set the agent could not see; here both directories are
  present in the workspace, which slightly lowers difficulty vs. upstream.)
- `checker.sh` + `checker_data/run_checks.py`: a pure-stdlib re-implementation of
  the upstream `tests/test_outputs.py`. It runs each program directly through
  the reference interpreter and through the agent's `eval.scm` (and, for
  `05-simple*`, `calculator.scm`, `closures.scm`, through a randomized
  checker-scratch copy of `eval.scm` interpreting the target program) and
  requires identical output. Randomizing the nested evaluator filename hardens
  the self-hosting check against solutions that special-case the literal
  `eval.scm` name as a transparent pass-through. pytest is not used.
  Per-run timeouts were raised (direct 15 s, via-eval 90 s) to absorb the
  slower minimal-image CPU.
- **Checker-owned oracle**: grading uses `checker_data/interp_ref.py` and the
  test programs under `checker_data/{test,shadow_test}/` (read-only task dir) —
  never the agent-editable workspace copies. The only workspace artifact the
  checker reads is the deliverable `eval.scm`, so doctoring `interp.py` or the
  test programs cannot influence the score. The workspace copies remain for the
  agent's own iteration.
- `solution/eval.scm`: the reference evaluator extracted verbatim from the
  upstream `solution.sh`.
