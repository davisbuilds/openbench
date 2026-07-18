# Fix the workflow engine test suite

`workflow/` is a small finite state-machine engine plus a concrete
order-fulfillment workflow built on top of it. The test suite under `tests/`
is currently failing in several places.

Run it from the project root:

```
python3 -m unittest
```

Some of the failures are misleading: a test may blow up pointing at one part of
the code (for example an `InvalidTransition` that looks like a missing entry in
the transition table, or an assertion about the order's audit trail) when the
real cause is elsewhere in how events are sequenced. Read the tests carefully,
reproduce each failure, trace it to its root cause, and fix the code under
`workflow/` until the whole suite passes.

The tests describe the intended behavior — treat them as the specification and
do not modify them.

Done when `python3 -m unittest` reports every test passing.
