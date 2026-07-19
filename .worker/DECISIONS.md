# Decisions

- The opt-in smoke runs exactly once per `bench/run.py` invocation, using the first selected harness (and its candidate, if applicable), because the requirement specifies one cell even though the CLI accepts multiple harnesses. The canonical repository `tasks/make-it-run` fixture is used even when `--tasks-dir` points at a custom suite; the main run's model, timeout, checker timeout, execution backend, image/fallback, proxy, version-drift, adapter, and candidate settings are reused.
- The preflight sidecar replaces the results file's final extension with `.preflight.jsonl` (for example, `results.jsonl` becomes `results.preflight.jsonl`), keeping it next to the main results file.
- `--max-consecutive-infra 0` disables the default-on circuit breaker; positive values set the threshold. Negative values are rejected.
- Near-zero spend uses the requested aggregate `tokens` field only: `None` or a numeric value below 100. Non-numeric token values are treated as real/unknown spend and do not trip a safety gate.
