"""Transition table for the state-machine engine.

A transition is a directed edge ``source --event--> target`` with an optional
``guard`` (a predicate deciding whether the edge is enabled) and an optional
``action`` (run when the edge is taken). Several transitions may share the
same ``(source, event)`` key; they are stored in **insertion order** so the
engine can pick the first one whose guard passes.
"""


class Transition:
    """A single edge in the machine."""

    def __init__(self, source, event, target, guard=None, action=None):
        self.source = source
        self.event = event
        self.target = target
        self.guard = guard
        self.action = action

    def __repr__(self):
        return "Transition({!r} --{!r}--> {!r})".format(
            self.source, self.event, self.target
        )


class TransitionTable:
    """Lookup structure mapping ``(source, event)`` to ordered candidates."""

    def __init__(self):
        # (source, event) -> list[Transition], preserving add() order.
        self._by_key = {}

    def add(self, source, event, target, guard=None, action=None):
        transition = Transition(source, event, target, guard=guard, action=action)
        self._by_key.setdefault((source, event), []).append(transition)
        return transition

    def candidates(self, source, event):
        """Return the transitions registered for ``(source, event)``.

        The list is a copy in insertion order (possibly empty). Guard
        evaluation is the caller's job -- the table only knows structure, not
        runtime enablement.
        """
        return list(self._by_key.get((source, event), []))

    def all(self):
        """Return every registered transition (flattened)."""
        out = []
        for transitions in self._by_key.values():
            out.extend(transitions)
        return out
