"""A small, generic finite state-machine engine.

The engine is deliberately minimal but supports the pieces a real workflow
needs:

* named states, each with optional ``on_entry`` / ``on_exit`` callables,
* guarded transitions -- several transitions may share the same
  ``(state, event)`` pair and are disambiguated by their guards, with the
  first guard that passes (in insertion order) winning,
* entry/exit actions that receive the machine, so an action MAY itself call
  :meth:`StateMachine.fire` to raise a *nested* (re-entrant) event.

Nested events are the subtle part. The engine uses **run-to-completion**
semantics: while one event is being processed, any event raised by an action
is placed on an internal queue and handled only after the current transition
has fully finished. Every event is therefore evaluated against the fully
updated ``current_state`` -- never a half-applied one.

Callable contracts
-------------------
* guard:  ``guard(machine, **event_kwargs) -> bool``
* action (entry/exit/transition): ``action(machine) -> None``
"""

from workflow.transitions import TransitionTable
from workflow.history import History


class InvalidTransition(Exception):
    """Raised when ``fire`` finds no enabled transition for the current state.

    "Enabled" means: a transition exists for ``(current_state, event)`` *and*
    its guard (if any) returns truthy. The offending state/event are attached
    for callers that want to inspect them.
    """

    def __init__(self, state, event):
        super().__init__(
            "no enabled transition from state {!r} on event {!r}".format(state, event)
        )
        self.state = state
        self.event = event


class State:
    """A named state plus its optional entry/exit actions."""

    def __init__(self, name, on_entry=None, on_exit=None):
        self.name = name
        self.on_entry = on_entry
        self.on_exit = on_exit

    def __repr__(self):
        return "State({!r})".format(self.name)


class StateMachine:
    """A finite state machine with guarded, action-bearing transitions.

    Parameters
    ----------
    initial:
        Name of the starting state. The state is created if it has not been
        registered yet. The initial state's ``on_entry`` action is **not**
        run at construction time -- entry actions run only when a transition
        moves *into* a state.
    """

    def __init__(self, initial):
        self.states = {}
        self.initial = initial
        self.current_state = initial
        self.table = TransitionTable()
        self.history = History()
        # Arbitrary caller-supplied context object (e.g. the order under
        # workflow). The engine never inspects it; guards/actions do.
        self.context = None
        self._ensure_state(initial)

    # -- registration ----------------------------------------------------

    def _ensure_state(self, name):
        if name not in self.states:
            self.states[name] = State(name)
        return self.states[name]

    def add_state(self, name, on_entry=None, on_exit=None):
        """Register (or update) a state and its entry/exit actions."""
        state = self._ensure_state(name)
        if on_entry is not None:
            state.on_entry = on_entry
        if on_exit is not None:
            state.on_exit = on_exit
        return state

    def add_transition(self, source, event, target, guard=None, action=None):
        """Add a transition ``source --event[/guard]--> target``.

        Multiple transitions may share ``(source, event)``; at fire time the
        first whose guard passes (insertion order) is taken.
        """
        self._ensure_state(source)
        self._ensure_state(target)
        self.table.add(source, event, target, guard=guard, action=action)

    # -- firing ----------------------------------------------------------

    def fire(self, event, **kwargs):
        """Raise ``event`` on the machine and run the enabled transition.

        An action running mid-transition may call ``fire`` again to raise a
        nested event; that nested event is handled as part of completing the
        outer one.
        """
        self._step(event, dict(kwargs))

    def _step(self, event, kwargs):
        """Process one event against the current state."""
        candidates = self.table.candidates(self.current_state, event)
        if not candidates:
            raise InvalidTransition(self.current_state, event)

        source_state = self.states[self.current_state]
        # Note the arrival up front so the path reflects the attempted move.
        self.history.record(candidates[0].target, event)
        # Leave the current state before selecting the enabled edge.
        if source_state.on_exit is not None:
            source_state.on_exit(self)

        chosen = None
        for transition in candidates:
            if transition.guard is None or transition.guard(self, **kwargs):
                chosen = transition
                break
        if chosen is None:
            raise InvalidTransition(self.current_state, event)

        target_state = self._ensure_state(chosen.target)
        if chosen.action is not None:
            chosen.action(self)
        if target_state.on_entry is not None:
            target_state.on_entry(self)
        self.current_state = chosen.target
