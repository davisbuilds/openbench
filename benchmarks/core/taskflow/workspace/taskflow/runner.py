"""Top-level entry point: run a pipeline described by a plain dictionary.

:func:`run_pipeline` ties the whole package together. Given a configuration
dictionary it:

1. loads and validates a :class:`~taskflow.model.Pipeline` (:mod:`taskflow.config`),
2. builds the dependency graph (:mod:`taskflow.dag`),
3. constructs the resource pools declared under ``pools`` (:mod:`taskflow.resources`),
4. wires a :class:`~taskflow.history.History` onto a fresh event bus,
5. runs the :class:`~taskflow.scheduler.Scheduler`, and
6. returns a :class:`RunReport` summarising the outcome.

The report exposes the final state of every task, the terminal-outcome sets,
and the observed run history, so a caller can assert on the result without
touching any internal object.
"""

from __future__ import annotations

from taskflow.config import load_pipeline
from taskflow.dag import Dag
from taskflow.events import EventBus
from taskflow.history import History
from taskflow.model import State
from taskflow.resources import ResourceManager
from taskflow.scheduler import Scheduler


class RunReport:
    """A read-only summary of a finished pipeline run.

    Wraps the scheduler's per-run records and the :class:`~taskflow.history.History`
    so callers get one object to query.
    """

    def __init__(self, pipeline, result, history):
        self.pipeline = pipeline
        self._result = result
        self.history = history

    @property
    def runs(self):
        """The ``{task_id: JobRun}`` mapping of every run."""

        return self._result.runs

    @property
    def ticks(self):
        """The number of virtual ticks the run consumed."""

        return self._result.ticks

    def state_of(self, task_id):
        """Return the final :class:`~taskflow.model.State` of ``task_id``."""

        return self._result.state_of(task_id)

    def attempts_of(self, task_id):
        """Return how many attempts ``task_id`` made."""

        return self._result.attempts_of(task_id)

    def states(self):
        """Return a ``{task_id: State}`` mapping of every final state."""

        return self._result.states()

    def succeeded(self):
        """Return the ids that ended in ``SUCCEEDED`` (pipeline order)."""

        return self._result.by_state(State.SUCCEEDED)

    def failed(self):
        """Return the ids that ended in ``FAILED`` (pipeline order)."""

        return self._result.by_state(State.FAILED)

    def skipped(self):
        """Return the ids that ended in ``SKIPPED`` (pipeline order)."""

        return self._result.by_state(State.SKIPPED)

    def run_order(self):
        """Return the order in which jobs first started (from history)."""

        return self.history.run_order()

    def ok(self):
        """Return ``True`` if every task succeeded."""

        return self._result.all_succeeded()

    def __repr__(self):
        return "RunReport(pipeline={!r}, ok={}, states={})".format(
            self.pipeline.name,
            self.ok(),
            {tid: str(s) for tid, s in self.states().items()},
        )


def _build_resources(config):
    """Construct a :class:`ResourceManager` from the ``pools`` config block."""

    pools = config.get("pools", {}) if isinstance(config, dict) else {}
    return ResourceManager(pools)


def run_pipeline(config, concurrency=None, max_ticks=None):
    """Load, run, and report on the pipeline described by ``config``.

    Parameters
    ----------
    config:
        The pipeline configuration dictionary (see :mod:`taskflow.config`). May
        additionally carry a top-level ``pools`` mapping of resource
        capacities and a ``concurrency`` integer.
    concurrency:
        Overrides the ``concurrency`` value in ``config`` when given.
    max_ticks:
        Overrides the scheduler's virtual-tick ceiling when given.
    """

    pipeline = load_pipeline(config)
    dag = Dag.from_pipeline(pipeline)

    resources = _build_resources(config)
    if concurrency is None and isinstance(config, dict):
        concurrency = config.get("concurrency")

    bus = EventBus()
    history = History(bus)

    scheduler_kwargs = {
        "dag": dag,
        "resources": resources,
        "concurrency": concurrency,
        "bus": bus,
    }
    if max_ticks is not None:
        scheduler_kwargs["max_ticks"] = max_ticks

    scheduler = Scheduler(pipeline, **scheduler_kwargs)
    result = scheduler.run()

    return RunReport(pipeline, result, history)
