# Admission gate — final matrix (2026-07-11, post rule-hardening)

Gate: `bench/admission_gate.py` at 617cc78 (timing-sensitivity = hard fail; checker_data required only when referenced). Laptop host mode; per-machine docker-mode determinism cert in `admission-cert-2026-07-11-mini/`.

| Task | Verdict | Findings |
|---|---|---|
| `tasks-imported/terminal-bench/cancel-async-tasks` | FAIL | hard:timing_sensitivity |
| `tasks-imported/terminal-bench/feal-differential-cryptanalysis` | PASS | — |
| `tasks-imported/terminal-bench/llm-inference-batching-scheduler` | PASS | — |
| `tasks-imported/terminal-bench/schemelike-metacircular-eval` | PASS | — |
| `tasks/add-feature` | PASS | — |
| `tasks/build-a-cli` | PASS | — |
| `tasks/fix-failing-test` | PASS | — |
| `tasks/make-ci-green` | PASS | — |
| `tasks/make-it-run` | PASS | — |
| `tasks/misleading-error` | PASS-WITH-WARNINGS | warn:ownership.workspace_read; warn:ownership.workspace_read |
| `tasks/taskflow` | PASS | — |
| `tasks/webcore` | PASS | — |

- cancel-async-tasks FAIL is the intended negative control (timing patterns; task is DROPPED).
- misleading-error warns are the checker executing the program under test (expected value hardcoded in checker) — benign.
