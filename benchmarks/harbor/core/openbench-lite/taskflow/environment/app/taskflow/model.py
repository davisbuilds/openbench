"""Core data types for the :mod:`taskflow` orchestration engine.

This module defines the small vocabulary of value objects that every other
module in the package speaks in:

``State``
    The lifecycle states a single job run can occupy.

``Task``
    A unit of work: an identifier, an action callable, its dependencies, and
    the scheduling knobs (priority, retry policy, resource costs, timeout).

``Pipeline``
    A named collection of tasks plus the raw ``defaults`` block that was used
    to build them.

``JobRun``
    The mutable runtime record for one task inside one scheduler execution:
    its current state, how many attempts it has made, and a compact timeline
    of the states it has passed through.

``Event``
    An immutable notification published on the event bus.

Everything here is deliberately dependency-free (standard library only) and
deterministic. No wall-clock time, threads, or randomness ever leak in; the
scheduler drives a virtual clock and passes it down explicitly.
"""

from __future__ import annotations

import enum


class State(enum.Enum):
    """The lifecycle states a :class:`JobRun` moves through.

    The ordering of the members is meaningful only for stable, deterministic
    sorting and display; the legal transitions between states are defined in
    :mod:`taskflow.statemachine`, not here.
    """

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

    def is_terminal(self):
        """Return ``True`` if no further transition is expected from here.

        Terminal states are the resting states of a run: it has either
        finished (``SUCCEEDED``), given up (``FAILED``), been skipped because a
        dependency failed (``SKIPPED``), or been cancelled.
        """

        return self in _TERMINAL_STATES

    def is_active(self):
        """Return ``True`` while the run is still progressing.

        A run is active when it is waiting, dispatched, executing, or about to
        be retried -- i.e. any non-terminal state.
        """

        return self not in _TERMINAL_STATES

    def is_failure(self):
        """Return ``True`` for the two "did not succeed" terminal states.

        ``FAILED`` (the task gave up) and ``SKIPPED`` (an upstream task failed)
        are both non-success terminals; ``CANCELLED`` is treated separately
        because it reflects an external decision rather than a failure.
        """

        return self in _FAILURE_STATES

    @classmethod
    def from_string(cls, value):
        """Return the ``State`` whose value is ``value`` (case-insensitive)."""

        key = value.lower() if isinstance(value, str) else value
        for member in cls:
            if member.value == key:
                return member
        raise ValueError("unknown state: {!r}".format(value))

    @classmethod
    def terminal_states(cls):
        """Return the frozenset of terminal states."""

        return _TERMINAL_STATES

    def __str__(self):
        return self.value


_TERMINAL_STATES = frozenset(
    {State.SUCCEEDED, State.FAILED, State.SKIPPED, State.CANCELLED}
)

_FAILURE_STATES = frozenset({State.FAILED, State.SKIPPED})


class TaskflowError(Exception):
    """Base class for every error raised inside :mod:`taskflow`."""


class ConfigError(TaskflowError):
    """Raised when a pipeline configuration dictionary is invalid."""


class GraphError(TaskflowError):
    """Raised for structural problems in a dependency graph (e.g. cycles)."""


class Task:
    """A single unit of work in a pipeline.

    Parameters
    ----------
    task_id:
        Unique identifier within a pipeline.
    action:
        A callable invoked once per attempt. It receives a single
        :class:`JobContext` argument. Returning normally means the attempt
        succeeded; raising any exception means the attempt failed. ``None`` is
        treated as a no-op action that always succeeds.
    deps:
        Identifiers of tasks that must reach ``SUCCEEDED`` before this task may
        start.
    priority:
        Higher numbers are scheduled first. Ties are broken deterministically
        by insertion order in the ready queue.
    retry_policy:
        A :class:`taskflow.retry.RetryPolicy` deciding whether a failed attempt
        is retried.
    resource_costs:
        Mapping of resource-pool name to the integer units this task holds
        while running.
    timeout:
        Optional virtual-time budget for a single attempt. Purely advisory in
        this in-memory engine; recorded for reporting.
    duration:
        Number of virtual ticks a single attempt occupies while ``RUNNING``.
    metadata:
        Free-form dictionary carried through untouched for callers.
    """

    __slots__ = (
        "id",
        "action",
        "deps",
        "priority",
        "retry_policy",
        "resource_costs",
        "timeout",
        "duration",
        "metadata",
    )

    def __init__(
        self,
        task_id,
        action=None,
        deps=None,
        priority=0,
        retry_policy=None,
        resource_costs=None,
        timeout=None,
        duration=1,
        metadata=None,
    ):
        if not isinstance(task_id, str) or not task_id:
            raise ConfigError("task id must be a non-empty string")
        self.id = task_id
        self.action = action
        self.deps = list(deps) if deps else []
        self.priority = int(priority)
        # Imported lazily to avoid a circular import at module load time.
        if retry_policy is None:
            from taskflow.retry import RetryPolicy

            retry_policy = RetryPolicy()
        self.retry_policy = retry_policy
        self.resource_costs = dict(resource_costs) if resource_costs else {}
        self.timeout = timeout
        self.duration = int(duration)
        if self.duration < 1:
            raise ConfigError(
                "task {!r} duration must be >= 1".format(task_id)
            )
        self.metadata = dict(metadata) if metadata else {}

    def total_resource_cost(self):
        """Return the sum of every resource unit this task holds while running."""

        return sum(self.resource_costs.values())

    def requires(self, pool_name):
        """Return the integer cost this task charges to ``pool_name`` (0 if none)."""

        return self.resource_costs.get(pool_name, 0)

    def uses_resources(self):
        """Return ``True`` if this task charges any resource pool a positive cost."""

        return any(cost > 0 for cost in self.resource_costs.values())

    def with_overrides(self, **changes):
        """Return a shallow copy of this task with the given fields replaced.

        Only the recognised construction fields may be overridden; anything
        else raises :class:`ConfigError`. The original task is left untouched,
        which keeps a loaded pipeline immutable from a caller's point of view.
        """

        fields = {
            "action": self.action,
            "deps": list(self.deps),
            "priority": self.priority,
            "retry_policy": self.retry_policy,
            "resource_costs": dict(self.resource_costs),
            "timeout": self.timeout,
            "duration": self.duration,
            "metadata": dict(self.metadata),
        }
        for key, value in changes.items():
            if key not in fields:
                raise ConfigError("unknown task field: {!r}".format(key))
            fields[key] = value
        return Task(self.id, **fields)

    def describe(self):
        """Return a one-line human-readable summary of the task's scheduling."""

        parts = ["priority={}".format(self.priority)]
        if self.deps:
            parts.append("deps={}".format(",".join(self.deps)))
        if self.uses_resources():
            parts.append("resources={}".format(self.resource_costs))
        parts.append("retry={}".format(self.retry_policy.max_attempts))
        return "Task {!r}: {}".format(self.id, ", ".join(parts))

    def __repr__(self):
        return "Task(id={!r}, priority={}, deps={})".format(
            self.id, self.priority, self.deps
        )


class Pipeline:
    """A named, ordered collection of :class:`Task` objects.

    The pipeline preserves the insertion order of its tasks, which is the
    deterministic tie-breaker used throughout the engine when two tasks are
    otherwise indistinguishable (e.g. equal priority in the ready queue).
    """

    __slots__ = ("name", "_tasks", "defaults")

    def __init__(self, name, tasks=None, defaults=None):
        self.name = name
        self._tasks = {}
        self.defaults = dict(defaults) if defaults else {}
        if tasks:
            for task in tasks:
                self.add(task)

    def add(self, task):
        """Register ``task`` in the pipeline, preserving insertion order."""

        if task.id in self._tasks:
            raise ConfigError("duplicate task id: {!r}".format(task.id))
        self._tasks[task.id] = task
        return task

    def get(self, task_id):
        """Return the task with ``task_id`` or raise :class:`KeyError`."""

        return self._tasks[task_id]

    def has(self, task_id):
        """Return ``True`` if ``task_id`` is present in the pipeline."""

        return task_id in self._tasks

    def tasks(self):
        """Return the tasks in deterministic insertion order."""

        return list(self._tasks.values())

    def task_ids(self):
        """Return the task identifiers in insertion order."""

        return list(self._tasks.keys())

    def remove(self, task_id):
        """Remove and return the task with ``task_id``.

        Any other task that still declares ``task_id`` as a dependency is left
        untouched; callers that care about referential integrity should validate
        afterwards (as :func:`taskflow.config.load_pipeline` does).
        """

        if task_id not in self._tasks:
            raise KeyError(task_id)
        return self._tasks.pop(task_id)

    def filter(self, predicate):
        """Return the tasks for which ``predicate(task)`` is truthy, in order."""

        return [task for task in self._tasks.values() if predicate(task)]

    def edges(self):
        """Return every ``(dependency, task_id)`` pair declared by the tasks."""

        pairs = []
        for task in self._tasks.values():
            for dep in task.deps:
                pairs.append((dep, task.id))
        return pairs

    def total_resource_demand(self):
        """Return the summed resource costs across every task, per pool.

        This is the demand if *every* task ran at once -- an upper bound the
        scheduler never actually reaches, but a useful sanity figure when
        sizing resource pools.
        """

        demand = {}
        for task in self._tasks.values():
            for pool, cost in task.resource_costs.items():
                demand[pool] = demand.get(pool, 0) + cost
        return demand

    def __len__(self):
        return len(self._tasks)

    def __iter__(self):
        return iter(self._tasks.values())

    def __repr__(self):
        return "Pipeline(name={!r}, tasks={})".format(
            self.name, self.task_ids()
        )


class JobContext:
    """The single argument handed to a task's action on each attempt.

    It exposes read-only-ish information the action may want: the task id, the
    current attempt number (1-based), and the virtual time the attempt began.
    Actions in tests use ``attempt`` to implement behaviours such as "fail the
    first two attempts, then succeed".
    """

    __slots__ = ("task_id", "attempt", "now", "scratch")

    def __init__(self, task_id, attempt, now):
        self.task_id = task_id
        self.attempt = attempt
        self.now = now
        # A mutable scratch space actions may use to stash state across a run.
        self.scratch = {}

    def __repr__(self):
        return "JobContext(task_id={!r}, attempt={}, now={})".format(
            self.task_id, self.attempt, self.now
        )


class JobRun:
    """Mutable runtime record for one task within one scheduler execution.

    A ``JobRun`` starts in ``PENDING`` and is advanced through the lifecycle by
    the state machine as the scheduler drives it. It records how many attempts
    have been made and a compact timeline of ``(virtual_time, state)`` entries
    so that history queries can reconstruct what happened without replaying the
    whole event log.
    """

    __slots__ = (
        "task",
        "state",
        "attempts",
        "timeline",
        "admitted_at",
        "finish_at",
        "available_at",
        "last_error",
    )

    def __init__(self, task):
        self.task = task
        self.state = State.PENDING
        self.attempts = 0
        self.timeline = [(0, State.PENDING)]
        # Virtual times, filled in by the scheduler.
        self.admitted_at = None
        self.finish_at = None
        self.available_at = 0
        self.last_error = None

    @property
    def id(self):
        """The identifier of the underlying task."""

        return self.task.id

    @property
    def priority(self):
        """The scheduling priority of the underlying task."""

        return self.task.priority

    def record_state(self, state, now):
        """Set ``state`` and append a timeline entry stamped at ``now``.

        The state machine calls this after it has validated a transition, so
        this method performs no validation itself -- it is a pure recorder.
        """

        self.state = state
        self.timeline.append((now, state))

    def note_attempt(self):
        """Increment and return the attempt counter (1-based after first call)."""

        self.attempts += 1
        return self.attempts

    def states_seen(self):
        """Return the ordered list of distinct-in-sequence states visited."""

        return [state for _at, state in self.timeline]

    def entered_state_at(self, state):
        """Return the virtual time the run first entered ``state``, or ``None``."""

        for at, seen in self.timeline:
            if seen is state:
                return at
        return None

    def was_retried(self):
        """Return ``True`` if the run ever passed through ``RETRYING``."""

        return any(state is State.RETRYING for _at, state in self.timeline)

    def is_settled(self):
        """Return ``True`` if the run has reached a terminal state."""

        return self.state.is_terminal()

    def succeeded(self):
        """Return ``True`` if the run finished in ``SUCCEEDED``."""

        return self.state is State.SUCCEEDED

    def __repr__(self):
        return "JobRun(id={!r}, state={}, attempts={})".format(
            self.id, self.state, self.attempts
        )


class Event:
    """An immutable notification published on the :class:`~taskflow.events.EventBus`.

    Parameters
    ----------
    topic:
        A dotted topic string such as ``"job.started"`` or
        ``"job.term.failed"``. Subscribers may match a topic exactly or by a
        dotted prefix (see :mod:`taskflow.events`).
    payload:
        A mapping of arbitrary data describing the event.
    seq:
        A monotonically increasing sequence number assigned by the bus, giving
        every event a total, deterministic order.
    at:
        The virtual time at which the event was published.
    """

    __slots__ = ("topic", "payload", "seq", "at")

    def __init__(self, topic, payload=None, seq=0, at=0):
        self.topic = topic
        self.payload = dict(payload) if payload else {}
        self.seq = seq
        self.at = at

    def get(self, key, default=None):
        """Convenience accessor for ``payload`` entries."""

        return self.payload.get(key, default)

    def __repr__(self):
        return "Event(topic={!r}, seq={}, at={}, payload={})".format(
            self.topic, self.seq, self.at, self.payload
        )
