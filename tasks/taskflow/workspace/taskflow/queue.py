"""A deterministic ready-queue for tasks whose dependencies are satisfied.

The scheduler pushes a task onto the :class:`ReadyQueue` the moment all of its
dependencies have succeeded, and pops tasks off it when it has capacity to run
them. The queue's ordering *is* the scheduling policy:

* **Higher priority first.** A task with priority 5 is always dispatched before
  a task with priority 1.
* **Insertion order breaks ties.** Among tasks of equal priority, the one that
  became ready first is dispatched first (FIFO). This keeps the schedule
  stable and reproducible run to run.

The queue is a thin, explicit structure rather than a binary heap so the
tie-breaking rules are obvious and auditable. Every entry carries a
monotonically increasing sequence number assigned at push time; ordering is a
lexicographic sort on ``(-priority, sequence)``.
"""

from __future__ import annotations


class _Entry:
    """One queued item: the task id, its priority, and its insertion sequence."""

    __slots__ = ("task_id", "priority", "seq")

    def __init__(self, task_id, priority, seq):
        self.task_id = task_id
        self.priority = priority
        self.seq = seq

    def sort_key(self):
        """Return the ordering key: higher priority first, then FIFO.

        Priority is negated so that a plain ascending sort places the
        highest-priority entry first; the insertion sequence then orders equal
        priorities in first-in-first-out fashion.
        """

        return (self.seq, -self.priority)

    def __repr__(self):
        return "_Entry(task_id={!r}, priority={}, seq={})".format(
            self.task_id, self.priority, self.seq
        )


class ReadyQueue:
    """A priority-ordered, insertion-stable queue of ready task ids.

    The queue never holds the same task id twice at once: pushing an id that is
    already queued is a no-op (the scheduler may re-offer a task across ticks).
    Sequence numbers keep increasing across the whole lifetime of the queue so
    that a task re-queued after a retry sorts *after* tasks already waiting at
    the same priority -- it goes to the back of its priority band, which is the
    fair behaviour for a retry.
    """

    def __init__(self):
        self._entries = []
        self._queued = set()
        self._counter = 0

    def push(self, task_id, priority=0):
        """Add ``task_id`` at ``priority``. A no-op if already queued.

        Returns ``True`` if the id was added, ``False`` if it was already
        present.
        """

        if task_id in self._queued:
            return False
        entry = _Entry(task_id, priority, self._counter)
        self._counter += 1
        self._entries.append(entry)
        self._queued.add(task_id)
        return True

    def requeue(self, task_id, priority=0):
        """Re-add ``task_id`` after a retry, sending it to the back of its band.

        Because sequence numbers only ever increase, a re-queued task sorts
        after every task currently waiting at the same priority. This is a
        thin wrapper over :meth:`push` kept as a separate name so scheduler
        code reads clearly at the call site.
        """

        return self.push(task_id, priority)

    def pop(self):
        """Remove and return the highest-priority, earliest-inserted task id.

        Raises :class:`IndexError` if the queue is empty. The winning entry is
        selected by scanning for the minimum ``sort_key`` -- higher priority
        first, then lower insertion sequence.
        """

        if not self._entries:
            raise IndexError("pop from an empty ReadyQueue")
        best_index = 0
        best_key = self._entries[0].sort_key()
        for i in range(1, len(self._entries)):
            key = self._entries[i].sort_key()
            if key < best_key:
                best_key = key
                best_index = i
        entry = self._entries.pop(best_index)
        self._queued.discard(entry.task_id)
        return entry.task_id

    def remove(self, task_id):
        """Remove ``task_id`` from the queue if present. Returns ``True`` if removed.

        The scheduler calls this when a queued task is skipped because one of
        its dependencies failed, so a doomed task is never dispatched.
        """

        if task_id not in self._queued:
            return False
        for i, entry in enumerate(self._entries):
            if entry.task_id == task_id:
                self._entries.pop(i)
                break
        self._queued.discard(task_id)
        return True

    def peek(self):
        """Return the id that :meth:`pop` would return, without removing it."""

        if not self._entries:
            raise IndexError("peek at an empty ReadyQueue")
        best = self._entries[0]
        best_key = best.sort_key()
        for entry in self._entries[1:]:
            key = entry.sort_key()
            if key < best_key:
                best = entry
                best_key = key
        return best.task_id

    def drain(self):
        """Pop and return every task id in dispatch order as a list."""

        ordered = []
        while self._entries:
            ordered.append(self.pop())
        return ordered

    def ordered_ids(self):
        """Return the queued ids in dispatch order without modifying the queue."""

        return [e.task_id for e in sorted(self._entries, key=_Entry.sort_key)]

    def __contains__(self, task_id):
        return task_id in self._queued

    def __len__(self):
        return len(self._entries)

    def __bool__(self):
        return bool(self._entries)

    def __repr__(self):
        return "ReadyQueue({})".format(self.ordered_ids())
