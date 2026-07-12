# Determinism certification — Mac mini, docker mode (2026-07-11)

Node: Matthews-Mac-mini.local | exec: `docker run --cpus 4` | image: `sha256:a6fc4415be102e...` (full digest + timestamps in CERT_STAMP.txt)
Config: 20 solution runs + 10 bare-workspace runs per task, 6 CPU stress burners. Raw per-run JSON in this directory.

| Task | Determinism | Runs (sol/ws) | Solution wall min/med/max (s) |
|---|---|---|---|
| `tasks-imported/terminal-bench/cancel-async-tasks` | PASS | 20/10 | 12.56/12.6/12.72 |
| `tasks-imported/terminal-bench/feal-differential-cryptanalysis` | PASS | 20/10 | 4.38/4.47/4.61 |
| `tasks-imported/terminal-bench/llm-inference-batching-scheduler` | PASS | 20/10 | 0.27/0.31/0.43 |
| `tasks-imported/terminal-bench/schemelike-metacircular-eval` | PASS | 20/10 | 62.84/63.38/65.02 |
| `tasks/add-feature` | PASS | 20/10 | 0.24/0.26/0.34 |
| `tasks/build-a-cli` | PASS | 20/10 | 0.23/0.28/0.3 |
| `tasks/fix-failing-test` | PASS | 20/10 | 0.22/0.24/0.26 |
| `tasks/make-ci-green` | PASS | 20/10 | 0.24/0.26/0.37 |
| `tasks/make-it-run` | PASS | 20/10 | 0.19/0.24/0.27 |
| `tasks/misleading-error` | PASS | 20/10 | 0.21/0.25/0.26 |
| `tasks/taskflow` | PASS | 20/10 | 0.25/0.27/0.29 |
| `tasks/webcore` | PASS | 20/10 | 0.28/0.28/0.3 |

Note: cancel-async-tasks passes synthetic-stress determinism here too — consistent with the laptop finding that CPU burners do not reproduce its real batch-load flakiness. It remains DROPPED on static timing-sensitivity grounds; this cert does not re-admit it.
