# Get the scheduler test suite passing

`scheduler/` is a small dependency-aware job-scheduler library: a dependency
graph, jobs with priorities and retry budgets, a fixed-capacity resource pool, a
deterministic run loop, and an event-history log. It ships with a unittest suite
under `tests/` that is currently failing in several places.

Run it from the workspace root:

```
python3 -m unittest
```

Work through the failures and fix the code in `scheduler/` until the whole suite
passes. Some failures are more subtle than they first look — a test named for one
module can fail because of a defect in another — so trace each one to its real
cause rather than patching the symptom.

The tests describe the intended behavior: treat them as the specification and do
not modify them.

Done when `python3 -m unittest` reports every test passing.
