"""An event-sourced record of what happened during a scheduler run.

:class:`History` subscribes to the :class:`~taskflow.events.EventBus` and folds
the event stream into queryable structures. It never inspects the scheduler's
internal state directly -- everything it knows, it learned from an event -- so
it doubles as a check that the scheduler narrates its work faithfully.

The queries it answers:

* :meth:`History.run_order` -- the order in which jobs first started running.
* :meth:`History.attempts_for` -- how many times a given job started (i.e. how
  many attempts it made, counting retries).
* :meth:`History.state_timeline` -- the ``(virtual_time, state)`` sequence a
  job passed through.
* :meth:`History.succeeded_jobs` / :meth:`History.failed_jobs` /
  :meth:`History.skipped_jobs` -- the terminal outcome sets.
* :meth:`History.peak_resource_usage` -- the high-water mark of a resource pool.

Topics consumed
---------------
``job.started``
    Exact subscription. Feeds run order, attempt counts and per-job start log.
``job.state``
    Exact subscription. Feeds the per-job state timeline.
``job.term`` (prefix)
    Prefix subscription covering ``job.term.succeeded`` / ``job.term.failed`` /
    ``job.term.skipped``. Feeds the terminal-outcome sets.
``resource.acquired`` / ``resource.released``
    Exact subscriptions. Feed the running resource tally and its peak.
"""

from __future__ import annotations

from taskflow.model import State


class History:
    """Fold a scheduler's event stream into queryable run history.

    Construct with an :class:`~taskflow.events.EventBus` and the history wires
    up all of its subscriptions immediately, so it captures every event
    published from that point on.
    """

    def __init__(self, bus):
        self._bus = bus

        self._start_order = []          # ids in first-start order
        self._start_seen = set()
        self._attempts = {}             # id -> number of starts
        self._timeline = {}             # id -> list of (at, State)

        self._succeeded = []            # ids, in the order they succeeded
        self._failed = []
        self._skipped = []

        self._usage = {}                # pool -> current units held
        self._peak = {}                 # pool -> peak units held
        self._events = []               # every event, for debugging/queries

        self._subscribe()

    # -- wiring -----------------------------------------------------------

    def _subscribe(self):
        self._bus.subscribe("job.started", self._on_started)
        self._bus.subscribe("job.state", self._on_state)
        self._bus.subscribe_prefix("job.term", self._on_terminal)
        self._bus.subscribe("resource.acquired", self._on_acquired)
        self._bus.subscribe("resource.released", self._on_released)

    # -- handlers ---------------------------------------------------------

    def _on_started(self, event):
        self._events.append(event)
        task_id = event.get("id")
        self._attempts[task_id] = self._attempts.get(task_id, 0) + 1
        if task_id not in self._start_seen:
            self._start_seen.add(task_id)
            self._start_order.append(task_id)

    def _on_state(self, event):
        self._events.append(event)
        task_id = event.get("id")
        state = event.get("state")
        self._timeline.setdefault(task_id, []).append((event.at, state))

    def _on_terminal(self, event):
        self._events.append(event)
        task_id = event.get("id")
        suffix = event.topic.rsplit(".", 1)[-1]
        if suffix == "succeeded":
            self._succeeded.append(task_id)
        elif suffix == "failed":
            self._failed.append(task_id)
        elif suffix == "skipped":
            self._skipped.append(task_id)

    def _on_acquired(self, event):
        self._events.append(event)
        pool = event.get("pool")
        cost = event.get("cost", 0)
        self._usage[pool] = self._usage.get(pool, 0) + cost
        if self._usage[pool] > self._peak.get(pool, 0):
            self._peak[pool] = self._usage[pool]

    def _on_released(self, event):
        self._events.append(event)
        pool = event.get("pool")
        cost = event.get("cost", 0)
        self._usage[pool] = self._usage.get(pool, 0) - cost

    # -- queries ----------------------------------------------------------

    def run_order(self):
        """Return the ids of jobs in the order they first started running."""

        return list(self._start_order)

    def attempts_for(self, task_id):
        """Return how many times ``task_id`` started (attempts, incl. retries)."""

        return self._attempts.get(task_id, 0)

    def state_timeline(self, task_id):
        """Return the ``(virtual_time, State)`` entries recorded for ``task_id``."""

        return list(self._timeline.get(task_id, []))

    def final_state(self, task_id):
        """Return the last state recorded for ``task_id``, or ``None`` if unseen."""

        entries = self._timeline.get(task_id)
        return entries[-1][1] if entries else None

    def succeeded_jobs(self):
        """Return the ids that reached ``SUCCEEDED``, in completion order."""

        return list(self._succeeded)

    def failed_jobs(self):
        """Return the ids that reached ``FAILED``, in completion order."""

        return list(self._failed)

    def skipped_jobs(self):
        """Return the ids that were ``SKIPPED``, in the order they were skipped."""

        return list(self._skipped)

    def peak_resource_usage(self, pool):
        """Return the high-water mark of concurrent units held in ``pool``."""

        return self._peak.get(pool, 0)

    def current_resource_usage(self, pool):
        """Return the units currently held in ``pool`` (0 once a run is done)."""

        return self._usage.get(pool, 0)

    def events(self):
        """Return every event the history observed, in delivery order."""

        return list(self._events)

    def events_on(self, topic):
        """Return every observed event whose topic exactly equals ``topic``."""

        return [event for event in self._events if event.topic == topic]

    def total_attempts(self):
        """Return the total number of attempts across every job (incl. retries)."""

        return sum(self._attempts.values())

    def retried_jobs(self):
        """Return the ids of jobs that started more than once (were retried)."""

        return [tid for tid, n in self._attempts.items() if n > 1]

    def outcome_counts(self):
        """Return a ``{outcome: count}`` tally of terminal outcomes.

        The keys are ``"succeeded"``, ``"failed"`` and ``"skipped"``; the values
        are how many jobs reached each. Handy for a one-glance run summary.
        """

        return {
            "succeeded": len(self._succeeded),
            "failed": len(self._failed),
            "skipped": len(self._skipped),
        }

    def entered_state_at(self, task_id, state):
        """Return the virtual time ``task_id`` first entered ``state``, or ``None``."""

        for at, seen in self._timeline.get(task_id, []):
            if seen is state:
                return at
        return None

    def state_at(self, task_id, when):
        """Return the state ``task_id`` was in at virtual time ``when``.

        Returns the most recent state recorded at or before ``when``, or
        ``None`` if the job had not yet been seen by then.
        """

        result = None
        for at, seen in self._timeline.get(task_id, []):
            if at <= when:
                result = seen
            else:
                break
        return result

    def observed_jobs(self):
        """Return every task id the history has seen a state event for."""

        return list(self._timeline.keys())

    def resource_pools_seen(self):
        """Return the names of resource pools that saw any acquire event."""

        return list(self._peak.keys())

    def summary(self):
        """Return a compact dict summarising terminal outcomes and order."""

        return {
            "run_order": self.run_order(),
            "succeeded": self.succeeded_jobs(),
            "failed": self.failed_jobs(),
            "skipped": self.skipped_jobs(),
        }

    def __repr__(self):
        return (
            "History(started={}, succeeded={}, failed={}, skipped={})".format(
                len(self._start_order),
                len(self._succeeded),
                len(self._failed),
                len(self._skipped),
            )
        )
