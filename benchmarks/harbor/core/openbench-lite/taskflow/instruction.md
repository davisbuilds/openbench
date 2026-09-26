# Fix the failing tests in `taskflow`

`taskflow/` is an in-memory job-orchestration engine: it runs a pipeline of
interdependent tasks to completion on a virtual clock, with priority
scheduling, retries with backoff, resource-limited concurrency, a per-job state
machine, an event bus, and an event-sourced run history. The package has a
`tests/` suite that describes how all of this is supposed to behave.

Right now the suite is failing in several places. Run it from this directory:

```
python3 -m unittest
```

Work through the failures and fix the code under `taskflow/` until the whole
suite passes. Treat the tests as the specification — they encode the intended
behavior; do not modify them.

A few things worth knowing before you dive in:

- **The failures can be misleading.** A test named for one module may be
  failing because of a defect in a *different* module. Read each failure for
  what it actually asserts, then trace the behavior back to its root cause
  rather than patching the nearest line.
- **Fixing one bug can uncover another.** Some defects are masked by others:
  correcting the first makes a second one visible (a test that was failing for
  reason A starts failing for reason B). Expect to fix, re-run, and re-diagnose
  more than once.
- **The defects are genuine logic errors**, not typos that raise on import. The
  code reads as intended-but-wrong: comments and docstrings describe the
  correct behavior, so compare what each function *says* it does against what it
  *actually* does.

Done when `python3 -m unittest` reports every test passing.
