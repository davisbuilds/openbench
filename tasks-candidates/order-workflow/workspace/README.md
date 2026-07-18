# workflow

A small finite state-machine engine and an order-fulfillment workflow built on
it.

## Package layout

- `workflow/machine.py` — the generic `StateMachine`: states, guarded
  transitions, entry/exit actions, and event firing (`fire`). Actions receive
  the machine, so an action may `fire` a nested event.
- `workflow/transitions.py` — the transition table and `(state, event)` lookup.
- `workflow/history.py` — the ordered log of transitions taken, with
  `path()`, `was_visited(state)`, and `current()`.
- `workflow/order_workflow.py` — the concrete order workflow
  (`CREATED → PAID → PACKED → SHIPPED → DELIVERED`, plus cancel and backorder
  branches) built with `build(order)`.

## Running the tests

From this directory (the project root):

```
python3 -m unittest
```

The tests live under `tests/` and import the package as `workflow`.
