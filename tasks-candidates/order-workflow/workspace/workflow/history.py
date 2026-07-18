"""Transition history for the state-machine engine.

The history is an ordered log of the transitions the machine has actually
taken. Each entry is a ``(state, event)`` pair recording the state the machine
*arrived in* and the event that took it there. The initial state is not
logged (nothing transitioned into it), so a fresh machine has an empty
history.

Only *committed* transitions belong here: an event that is rejected (no
enabled transition) must not leave a trace.
"""


class History:
    def __init__(self):
        self._entries = []

    def record(self, state, event):
        """Append the arrival in ``state`` via ``event``."""
        self._entries.append((state, event))

    def path(self):
        """Return the ordered list of states arrived in."""
        return [state for state, _event in self._entries]

    def events(self):
        """Return the ordered list of events taken."""
        return [event for _state, event in self._entries]

    def entries(self):
        """Return the raw ``(state, event)`` log as a list."""
        return list(self._entries)

    def was_visited(self, state):
        """True iff the machine has transitioned into ``state`` at least once."""
        return any(s == state for s, _event in self._entries)

    def current(self):
        """The most recently arrived state, or ``None`` if nothing has run."""
        if not self._entries:
            return None
        return self._entries[-1][0]

    def __len__(self):
        return len(self._entries)
