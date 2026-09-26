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
        # Run-to-completion machinery. `_queue` holds events raised while an
        # event is already being processed; `_processing` guards the drain.
        self._queue = []
        self._processing = False
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
        """Raise ``event`` on the machine.

        If the machine is already processing an event (i.e. this call happens
        from inside an entry/exit/transition action), the event is queued and
        will be handled after the current transition completes -- this is the
        run-to-completion guarantee. Otherwise the event is processed
        immediately, followed by draining anything queued while it ran.
        """
        self._queue.append((event, dict(kwargs)))
        if self._processing:
            return
        self._processing = True
        try:
            while self._queue:
                next_event, next_kwargs = self._queue.pop(0)
                self._step(next_event, next_kwargs)
        finally:
            self._processing = False

    def _step(self, event, kwargs):
        """Process exactly one event against the *current* state.

        Order matters. The guard is evaluated first and, if nothing is
        enabled, we raise **before** touching the machine -- a rejected event
        must leave state, history, and side effects untouched. Only once a
        transition is committed do we run exit action, move state, record
        history, run the transition action, then the target's entry action
        (which may enqueue nested events).
        """
        candidates = self.table.candidates(self.current_state, event)
        if not candidates:
            raise InvalidTransition(self.current_state, event)

        chosen = None
        for transition in candidates:
            if transition.guard is None or transition.guard(self, **kwargs):
                chosen = transition
                break
        if chosen is None:
            # A transition exists but no guard passed: reject cleanly, with no
            # side effects of any kind.
            raise InvalidTransition(self.current_state, event)

        source_state = self.states[self.current_state]
        target_state = self._ensure_state(chosen.target)

        # Leave the old state, then commit the move, then record it, then run
        # the arriving actions.
        if source_state.on_exit is not None:
            source_state.on_exit(self)
        self.current_state = chosen.target
        self.history.record(chosen.target, event)
        if chosen.action is not None:
            chosen.action(self)
        if target_state.on_entry is not None:
            target_state.on_entry(self)
