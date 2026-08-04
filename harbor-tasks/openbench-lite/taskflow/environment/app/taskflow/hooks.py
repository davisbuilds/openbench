"""A lifecycle hook registry for observing an orchestration run.

The engine already narrates itself on an :class:`~taskflow.events.EventBus`, but
raw topic strings are a low-level interface. Tools and integrations usually want
a friendlier, named vocabulary: "run this callback before the pipeline starts",
"call me whenever any job fails", "notify me when the run finishes". This module
provides that vocabulary as a :class:`HookRegistry` layered over the same event
model.

A hook is a named point in the lifecycle -- see :class:`HookPoint` -- to which
any number of callbacks may be attached. Callbacks fire in registration order
and receive a :class:`HookContext` carrying the relevant payload. The registry
is deliberately decoupled from the scheduler: you can dispatch hooks by hand, or
bind the registry to an :class:`~taskflow.events.EventBus` with
:meth:`HookRegistry.bind_to_bus` so that job-lifecycle events are translated
into hook dispatches automatically.

A misbehaving callback should not silently corrupt a run, so exceptions raised
by callbacks are collected and re-raised together as a :class:`HookError` after
every callback for a point has been given a chance to run, unless the registry
is constructed with ``swallow_errors=True`` (useful for best-effort observers
like logging).
"""

from __future__ import annotations

import enum
from typing import Any, Callable, Dict, List, Optional


class HookPoint(enum.Enum):
    """The named points in a run's lifecycle a callback may attach to."""

    PIPELINE_START = "pipeline.start"
    PIPELINE_END = "pipeline.end"
    JOB_READY = "job.ready"
    JOB_START = "job.start"
    JOB_SUCCESS = "job.success"
    JOB_FAILURE = "job.failure"
    JOB_RETRY = "job.retry"
    JOB_SKIP = "job.skip"

    def __str__(self) -> str:
        return self.value


# The subset of hook points that concern an individual job (as opposed to the
# whole pipeline). Callbacks at these points can expect a ``task_id`` in context.
_JOB_POINTS = frozenset(
    {
        HookPoint.JOB_READY,
        HookPoint.JOB_START,
        HookPoint.JOB_SUCCESS,
        HookPoint.JOB_FAILURE,
        HookPoint.JOB_RETRY,
        HookPoint.JOB_SKIP,
    }
)


class HookError(Exception):
    """Aggregates the exceptions raised by callbacks at one dispatch.

    Carries the originating :class:`HookPoint` and the list of underlying
    exceptions so a caller can inspect exactly which callbacks failed without
    losing any of them.
    """

    def __init__(self, point: HookPoint, errors: List[BaseException]) -> None:
        self.point = point
        self.errors = list(errors)
        super().__init__(
            "{} callback(s) failed at hook {}".format(len(errors), point)
        )


class HookContext:
    """The single argument handed to every hook callback.

    Parameters
    ----------
    point:
        The :class:`HookPoint` being dispatched.
    task_id:
        The job the dispatch concerns, or ``None`` for pipeline-level points.
    now:
        The virtual time at dispatch.
    payload:
        A free-form mapping of extra data (attempt number, error, etc.).
    """

    __slots__ = ("point", "task_id", "now", "payload")

    def __init__(
        self,
        point: HookPoint,
        task_id: Optional[str] = None,
        now: int = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.point = point
        self.task_id = task_id
        self.now = now
        self.payload = dict(payload) if payload else {}

    def get(self, key: str, default: Any = None) -> Any:
        """Convenience accessor for a ``payload`` entry."""

        return self.payload.get(key, default)

    def __repr__(self) -> str:
        return "HookContext(point={}, task_id={!r}, now={})".format(
            self.point, self.task_id, self.now
        )


HookCallback = Callable[[HookContext], None]


class HookRegistry:
    """A registry mapping :class:`HookPoint` values to ordered callbacks.

    Register callbacks with :meth:`on` (or the point-specific convenience
    methods), then either :meth:`dispatch` points by hand or wire the registry
    to an event bus. Registration returns a small handle so an individual
    callback can later be removed.
    """

    def __init__(self, swallow_errors: bool = False) -> None:
        self._callbacks: Dict[HookPoint, List[HookCallback]] = {
            point: [] for point in HookPoint
        }
        self._swallow_errors = swallow_errors
        self._dispatch_count: Dict[HookPoint, int] = {
            point: 0 for point in HookPoint
        }

    # -- registration -----------------------------------------------------

    def on(self, point: HookPoint, callback: HookCallback) -> HookCallback:
        """Register ``callback`` to fire at ``point``; returns the callback.

        Returning the callback unchanged lets :meth:`on` double as a decorator::

            @registry.on(HookPoint.JOB_FAILURE)
            def log_failure(ctx):
                ...
        """

        if not isinstance(point, HookPoint):
            raise TypeError("point must be a HookPoint, got {!r}".format(point))
        self._callbacks[point].append(callback)
        return callback

    def off(self, point: HookPoint, callback: HookCallback) -> bool:
        """Unregister ``callback`` from ``point``; returns ``True`` if removed."""

        bucket = self._callbacks[point]
        if callback in bucket:
            bucket.remove(callback)
            return True
        return False

    def clear(self, point: Optional[HookPoint] = None) -> None:
        """Remove every callback, or just those at ``point`` if given."""

        if point is None:
            for bucket in self._callbacks.values():
                bucket.clear()
        else:
            self._callbacks[point].clear()

    def callbacks(self, point: HookPoint) -> List[HookCallback]:
        """Return the callbacks registered at ``point``, in fire order."""

        return list(self._callbacks[point])

    def has_callbacks(self, point: HookPoint) -> bool:
        """Return ``True`` if any callback is registered at ``point``."""

        return bool(self._callbacks[point])

    # -- convenience registration ----------------------------------------

    def on_pipeline_start(self, callback: HookCallback) -> HookCallback:
        """Register a callback for :attr:`HookPoint.PIPELINE_START`."""

        return self.on(HookPoint.PIPELINE_START, callback)

    def on_pipeline_end(self, callback: HookCallback) -> HookCallback:
        """Register a callback for :attr:`HookPoint.PIPELINE_END`."""

        return self.on(HookPoint.PIPELINE_END, callback)

    def on_job_success(self, callback: HookCallback) -> HookCallback:
        """Register a callback for :attr:`HookPoint.JOB_SUCCESS`."""

        return self.on(HookPoint.JOB_SUCCESS, callback)

    def on_job_failure(self, callback: HookCallback) -> HookCallback:
        """Register a callback for :attr:`HookPoint.JOB_FAILURE`."""

        return self.on(HookPoint.JOB_FAILURE, callback)

    # -- dispatch ---------------------------------------------------------

    def dispatch(
        self,
        point: HookPoint,
        task_id: Optional[str] = None,
        now: int = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Fire every callback registered at ``point`` in registration order.

        Returns the number of callbacks invoked. Errors raised by callbacks are
        collected; once all have run they are re-raised as a single
        :class:`HookError` unless the registry swallows errors, in which case
        they are dropped and the dispatch count still reflects every attempt.
        """

        ctx = HookContext(point, task_id=task_id, now=now, payload=payload)
        errors: List[BaseException] = []
        fired = 0
        for callback in list(self._callbacks[point]):
            try:
                callback(ctx)
            except Exception as exc:  # observer callbacks must not corrupt a run
                if not self._swallow_errors:
                    errors.append(exc)
            fired += 1
        self._dispatch_count[point] += 1
        if errors:
            raise HookError(point, errors)
        return fired

    def dispatch_count(self, point: HookPoint) -> int:
        """Return how many times ``point`` has been dispatched."""

        return self._dispatch_count[point]

    # -- event-bus bridge -------------------------------------------------

    def bind_to_bus(self, bus: Any) -> List[Any]:
        """Translate an :class:`~taskflow.events.EventBus` into hook dispatches.

        Subscribes to the scheduler's job-lifecycle topics and maps each to the
        corresponding :class:`HookPoint`, so a caller who prefers the hook
        vocabulary gets it without the scheduler knowing hooks exist. Returns the
        list of :class:`~taskflow.events.Subscription` handles so the binding can
        later be cancelled.

        The bus carries no explicit pipeline start/end topic, so those two points
        are left for the caller to dispatch directly; everything per-job is wired
        here.
        """

        mapping = {
            "job.started": HookPoint.JOB_START,
            "job.term.succeeded": HookPoint.JOB_SUCCESS,
            "job.term.failed": HookPoint.JOB_FAILURE,
            "job.term.skipped": HookPoint.JOB_SKIP,
        }
        subs = []
        for topic, point in mapping.items():
            subs.append(bus.subscribe(topic, self._make_bridge(point)))
        return subs

    def _make_bridge(self, point: HookPoint) -> Callable[[Any], None]:
        """Return an event handler that redispatches an event as ``point``."""

        def handler(event: Any) -> None:
            self.dispatch(
                point,
                task_id=event.get("id"),
                now=event.at,
                payload=dict(event.payload),
            )

        return handler

    def __repr__(self) -> str:
        registered = sum(len(b) for b in self._callbacks.values())
        return "HookRegistry(callbacks={}, swallow_errors={})".format(
            registered, self._swallow_errors
        )
