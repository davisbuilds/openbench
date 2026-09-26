# scheduler

A small, dependency-aware job scheduler (pure Python standard library).

## Modules

- `scheduler/graph.py` — a directed dependency graph over job ids: dependencies,
  dependents, a priority-aware topological order, and cycle detection.
- `scheduler/jobs.py` — the `Job` record (id, deps, priority, retry budget,
  resource cost, run callable) and the `Status` lifecycle enum.
- `scheduler/resources.py` — a fixed-capacity `ResourcePool` jobs draw on while
  running; acquire/release with no over-admission.
- `scheduler/engine.py` — the deterministic, single-threaded run loop that
  schedules ready jobs by priority, honours concurrency and resource limits,
  retries failures, and skips the downstream of a permanent failure.
- `scheduler/history.py` — the ordered event log and query helpers
  (`run_order`, `retries_for`, `skipped_jobs`).

## Running the tests

From this directory:

```
python3 -m unittest
```
