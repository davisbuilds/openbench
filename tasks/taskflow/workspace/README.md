# taskflow

`taskflow` is a small, dependency-free, deterministic job-orchestration engine.
It takes a pipeline of interdependent tasks and runs them to completion on a
*virtual* clock — no threads, no sleeping, no wall-clock time or randomness — so
every run is reproducible.

## What it does

- Builds a pipeline from a plain Python dictionary and deep-merges a
  pipeline-wide `defaults` block into every task.
- Orders ready tasks by priority (ties broken by insertion order).
- Runs each task's action, retrying failed attempts according to a configurable
  retry policy with constant or exponential backoff.
- Bounds concurrency both by a global limit and by named resource pools with
  integer capacities.
- Drives every job through a validated state machine
  (`PENDING → READY → RUNNING → SUCCEEDED / FAILED / RETRYING / SKIPPED / CANCELLED`).
- When a task fails permanently, skips everything that transitively depends on
  it instead of running it.
- Narrates the whole run on an event bus, which an event-sourced `History`
  folds into queries (run order, attempts, terminal outcomes, peak resource
  usage).

## Layout

```
taskflow/
  model.py         core types: State, Task, Pipeline, JobRun, Event
  dag.py           dependency graph: levels, cycles, transitive dependents
  config.py        build a Pipeline from a dict; deep-merge defaults
  retry.py         RetryPolicy: backoff + retry-vs-permanent-fail decision
  queue.py         deterministic priority ready-queue
  resources.py     named resource pools + admission control
  statemachine.py  per-run lifecycle: transitions, guards, hooks
  scheduler.py     the virtual-clock orchestrator loop
  events.py        synchronous publish/subscribe event bus
  history.py       event-sourced run history and queries
  runner.py        run_pipeline(config) -> RunReport

  # analysis, tooling and presentation layers (built on the core above)
  stats.py           dependency-free descriptive-statistics helpers
  timeutil.py        virtual-clock helpers and backoff-curve utilities
  filters.py         composable Task/JobRun predicates (&, |, ~)
  hooks.py           lifecycle hook registry over the event bus
  dag_algorithms.py  advanced graph analyses layered on dag.Dag
  serialization.py   plain-dict snapshots of the core model objects
  validation.py      collect-everything config schema validation
  policies.py        pluggable scheduling policies (fifo/priority/fair-share)
  planner.py         dry-run execution plans, critical path, makespan estimates
  templating.py      parameterise and expand pipeline configs (${...}, for_each)
  metrics.py         counters/gauges/histograms/timers over the event stream
  diagnostics.py     explain why a run failed or skipped tasks
  reporting.py       render runs, plans and graphs as text / table / JSON / DOT
  cli.py             argparse front-end: validate / graph / plan / run
tests/             the unittest suite
```

The modules below the rule are pure additions layered on the core engine: they
read the core types and query surfaces but the scheduler never depends on them,
so the engine runs exactly the same with or without them.

## Running

From this directory:

```
python3 -m unittest
```

The typical entry point in code is `taskflow.run_pipeline`:

```python
from taskflow import run_pipeline

report = run_pipeline({
    "name": "demo",
    "tasks": [
        {"id": "a", "action": lambda ctx: None},
        {"id": "b", "action": lambda ctx: None, "deps": ["a"]},
    ],
})
assert report.ok()
```
