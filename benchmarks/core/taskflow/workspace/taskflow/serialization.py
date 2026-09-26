"""Plain-dict snapshots of the core model objects.

Everything the engine works with is an in-memory Python object, which is ideal
for running but awkward for the things around a run: caching a loaded pipeline,
diffing two configurations, logging the shape of a job at a point in time, or
handing a run's outcome to a non-Python consumer. This module converts the core
model types to and from JSON-friendly ``dict`` structures (only ``str``,
``int``, ``float``, ``bool``, ``None``, ``list`` and ``dict`` ever appear in the
output).

The one thing that cannot survive a round trip is a task's ``action`` callable:
functions are not data. A snapshot therefore records only whether an action was
present (``"has_action"``) and never tries to pickle it. When rebuilding a
:class:`~taskflow.model.Task` from a dict you may supply an ``actions`` mapping
from task id to callable to reattach behaviour; tasks without a supplied action
become no-ops, which is exactly what a structural snapshot (for planning or
reporting) wants.

The functions are intentionally symmetric where a round trip is meaningful:
``task_from_dict(task_to_dict(t))`` reproduces every field except the action,
and enums serialise to their string value and back.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from taskflow.model import Event, JobRun, Pipeline, State, Task
from taskflow.retry import RetryPolicy

ActionMap = Dict[str, Callable[[Any], Any]]


# -- State ----------------------------------------------------------------


def state_to_str(state: State) -> str:
    """Return the wire form of a :class:`~taskflow.model.State` (its value)."""

    return state.value


def state_from_str(value: str) -> State:
    """Return the :class:`~taskflow.model.State` for a serialised value."""

    return State.from_string(value)


# -- RetryPolicy ----------------------------------------------------------


def retry_to_dict(policy: RetryPolicy) -> Dict[str, Any]:
    """Return the retry policy as a plain dict.

    Delegates to the policy's own ``to_dict`` so the representation stays the
    single source of truth, but is exposed here for symmetry with the rest of
    the serialisation surface.
    """

    return policy.to_dict()


def retry_from_dict(data: Optional[Dict[str, Any]]) -> RetryPolicy:
    """Rebuild a :class:`~taskflow.retry.RetryPolicy` from a plain dict."""

    return RetryPolicy.from_dict(data)


# -- Task -----------------------------------------------------------------


def task_to_dict(task: Task) -> Dict[str, Any]:
    """Serialise a :class:`~taskflow.model.Task` to a JSON-friendly dict.

    Every structural and scheduling field is captured except the ``action``
    callable, which is represented only by the boolean ``has_action`` flag.
    Empty collections are still emitted so the shape is stable and diff-friendly.
    """

    return {
        "id": task.id,
        "has_action": task.action is not None,
        "deps": list(task.deps),
        "priority": task.priority,
        "retry": retry_to_dict(task.retry_policy),
        "resources": dict(task.resource_costs),
        "timeout": task.timeout,
        "duration": task.duration,
        "metadata": dict(task.metadata),
    }


def task_from_dict(
    data: Dict[str, Any], actions: Optional[ActionMap] = None
) -> Task:
    """Rebuild a :class:`~taskflow.model.Task` from :func:`task_to_dict` output.

    ``actions`` optionally maps a task id to the callable to reattach; a task
    absent from the map (or a ``None`` map) is rebuilt as a no-op. Unknown keys
    in ``data`` are ignored so a snapshot from a newer schema still loads.
    """

    action = None
    if actions is not None:
        action = actions.get(data["id"])
    return Task(
        data["id"],
        action=action,
        deps=list(data.get("deps", [])),
        priority=data.get("priority", 0),
        retry_policy=retry_from_dict(data.get("retry")),
        resource_costs=dict(data.get("resources", {})),
        timeout=data.get("timeout"),
        duration=data.get("duration", 1),
        metadata=dict(data.get("metadata", {})),
    )


# -- Pipeline -------------------------------------------------------------


def pipeline_to_dict(pipeline: Pipeline) -> Dict[str, Any]:
    """Serialise a whole :class:`~taskflow.model.Pipeline` to a dict.

    The result preserves task insertion order (as a list) and the pipeline's
    ``defaults`` block, so it is a faithful structural snapshot. It is close to,
    but not identical with, a :mod:`taskflow.config` input dict: this form
    carries each task's *already-merged* fields rather than the terse
    pre-merge form, which is what you want for inspection and diffing.
    """

    return {
        "name": pipeline.name,
        "defaults": dict(pipeline.defaults),
        "tasks": [task_to_dict(task) for task in pipeline.tasks()],
    }


def pipeline_from_dict(
    data: Dict[str, Any], actions: Optional[ActionMap] = None
) -> Pipeline:
    """Rebuild a :class:`~taskflow.model.Pipeline` from a snapshot dict.

    Tasks are re-added in their serialised order, preserving the deterministic
    tie-break ordering the engine relies on. Actions are reattached from the
    optional ``actions`` map exactly as in :func:`task_from_dict`.
    """

    pipeline = Pipeline(
        name=data.get("name", "pipeline"),
        defaults=dict(data.get("defaults", {})),
    )
    for task_data in data.get("tasks", []):
        pipeline.add(task_from_dict(task_data, actions=actions))
    return pipeline


# -- JobRun ---------------------------------------------------------------


def jobrun_to_dict(run: JobRun) -> Dict[str, Any]:
    """Snapshot a :class:`~taskflow.model.JobRun`'s runtime state to a dict.

    Captures the mutable run record -- current state, attempt count, the full
    ``(virtual_time, state)`` timeline and the scheduler's virtual-time markers
    -- so a finished (or in-flight) run can be logged or compared without
    holding a reference to the live object. The underlying task is captured by
    id only; pair this with :func:`task_to_dict` if the full task is needed.
    """

    return {
        "id": run.id,
        "state": state_to_str(run.state),
        "attempts": run.attempts,
        "timeline": [
            [at, state_to_str(state)] for at, state in run.timeline
        ],
        "admitted_at": run.admitted_at,
        "finish_at": run.finish_at,
        "available_at": run.available_at,
        "has_error": run.last_error is not None,
    }


def jobrun_from_dict(data: Dict[str, Any], task: Task) -> JobRun:
    """Rebuild a :class:`~taskflow.model.JobRun` from a snapshot and its task.

    Because a ``JobRun`` is inseparable from its ``Task`` (it exposes the task's
    id and priority), the caller must supply the reconstructed task. The
    timeline, attempt count and virtual-time markers are restored so the run
    reads as it did when snapshotted; the transient ``last_error`` object is not
    restored (only whether one was present is recorded).
    """

    run = JobRun(task)
    run.state = state_from_str(data["state"])
    run.attempts = data.get("attempts", 0)
    run.timeline = [
        (at, state_from_str(state)) for at, state in data.get("timeline", [])
    ]
    run.admitted_at = data.get("admitted_at")
    run.finish_at = data.get("finish_at")
    run.available_at = data.get("available_at", 0)
    return run


# -- Event ----------------------------------------------------------------


def event_to_dict(event: Event) -> Dict[str, Any]:
    """Serialise an :class:`~taskflow.model.Event`.

    Payload values that are :class:`~taskflow.model.State` enums are converted
    to their string form so the whole dict stays JSON-friendly; other payload
    values are passed through untouched (the scheduler only ever puts scalars
    and states in payloads).
    """

    payload: Dict[str, Any] = {}
    for key, value in event.payload.items():
        payload[key] = state_to_str(value) if isinstance(value, State) else value
    return {
        "topic": event.topic,
        "seq": event.seq,
        "at": event.at,
        "payload": payload,
    }


def event_from_dict(data: Dict[str, Any]) -> Event:
    """Rebuild an :class:`~taskflow.model.Event` from :func:`event_to_dict`.

    Payload values are passed through as-is; a caller that needs the ``state``
    payload back as an enum can convert it with :func:`state_from_str`, since the
    serialiser cannot know which keys were originally enums.
    """

    return Event(
        topic=data["topic"],
        payload=dict(data.get("payload", {})),
        seq=data.get("seq", 0),
        at=data.get("at", 0),
    )


def events_to_list(events: List[Event]) -> List[Dict[str, Any]]:
    """Serialise a list of events, preserving order."""

    return [event_to_dict(event) for event in events]


def events_from_list(rows: List[Dict[str, Any]]) -> List[Event]:
    """Rebuild a list of events from serialised rows, preserving order."""

    return [event_from_dict(row) for row in rows]
