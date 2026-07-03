"""The deterministic orchestrator loop.

The :class:`Scheduler` drives a whole pipeline to completion on a *virtual*
clock. There is no real time, no sleeping and no threads: each iteration of the
loop is one virtual tick, and the scheduler decides what happens at that tick by
inspecting the state of every job run.

Each tick proceeds in four phases, in this order:

1. **Wake retries.** Any run in ``RETRYING`` whose backoff delay has elapsed is
   returned to ``READY`` and re-queued.
2. **Complete.** Any ``RUNNING`` run whose finish time has arrived executes its
   action (one attempt). On success it becomes ``SUCCEEDED``; on failure it is
   either sent back to ``RETRYING`` (if its retry policy still permits an
   attempt) or to ``FAILED``. A permanent failure cascades: every task that
   transitively depends on the failed one is marked ``SKIPPED``.
3. **Unlock.** Any ``PENDING`` run whose dependencies have all ``SUCCEEDED``
   becomes ``READY`` and is pushed onto the ready queue.
4. **Admit.** While the concurrency limit and resource pools allow, the highest
   priority ready run is dispatched to ``RUNNING``, holding its declared
   resources until it completes.

The scheduler narrates every step on an :class:`~taskflow.events.EventBus` so a
:class:`~taskflow.history.History` (or any observer) can reconstruct the run.
"""

from __future__ import annotations

from taskflow.dag import Dag
from taskflow.events import EventBus
from taskflow.model import GraphError, JobContext, JobRun, State
from taskflow.resources import ResourceManager
from taskflow.queue import ReadyQueue
from taskflow.statemachine import IllegalTransition, StateMachine

# A generous ceiling on virtual ticks so a mis-configured pipeline cannot spin
# forever. Real pipelines settle in a number of ticks proportional to the graph
# depth; this only ever trips on a pathological configuration.
DEFAULT_MAX_TICKS = 10000


class SchedulerResult:
    """The outcome of a :meth:`Scheduler.run`, queryable by task id.

    Bundles the final :class:`~taskflow.model.JobRun` objects together with the
    :class:`~taskflow.history.History` and the resource manager, and offers a
    few convenience queries so tests and the runner do not have to reach into
    the internals.
    """

    def __init__(self, runs, history, bus, resources, ticks):
        self.runs = runs
        self.history = history
        self.bus = bus
        self.resources = resources
        self.ticks = ticks

    def state_of(self, task_id):
        """Return the final :class:`~taskflow.model.State` of ``task_id``."""

        return self.runs[task_id].state

    def attempts_of(self, task_id):
        """Return how many attempts ``task_id`` made."""

        return self.runs[task_id].attempts

    def states(self):
        """Return a ``{task_id: State}`` mapping of every run's final state."""

        return {tid: run.state for tid, run in self.runs.items()}

    def by_state(self, state):
        """Return the ids of runs that ended in ``state``, in insertion order."""

        return [tid for tid, run in self.runs.items() if run.state is state]

    def all_succeeded(self):
        """Return ``True`` if every run ended in ``SUCCEEDED``."""

        return all(run.state is State.SUCCEEDED for run in self.runs.values())

    def __repr__(self):
        return "SchedulerResult(ticks={}, states={})".format(
            self.ticks,
            {tid: str(run.state) for tid, run in self.runs.items()},
        )


class Scheduler:
    """Deterministically execute a pipeline on a virtual clock.

    Parameters
    ----------
    pipeline:
        The :class:`~taskflow.model.Pipeline` to run.
    dag:
        The dependency graph. Built from the pipeline when omitted.
    resources:
        A :class:`~taskflow.resources.ResourceManager`. An empty (unconstrained)
        manager is used when omitted.
    concurrency:
        The maximum number of runs allowed in ``RUNNING`` at once. ``None``
        means "no limit beyond what resources impose".
    bus:
        The :class:`~taskflow.events.EventBus` to narrate on. A fresh bus is
        created when omitted.
    max_ticks:
        Safety ceiling on the number of virtual ticks.
    """

    def __init__(
        self,
        pipeline,
        dag=None,
        resources=None,
        concurrency=None,
        bus=None,
        max_ticks=DEFAULT_MAX_TICKS,
    ):
        self.pipeline = pipeline
        self.dag = dag if dag is not None else Dag.from_pipeline(pipeline)
        self.resources = resources if resources is not None else ResourceManager()
        self.concurrency = concurrency
        self.bus = bus if bus is not None else EventBus()
        self.max_ticks = max_ticks

        self._runs = {}
        self._machines = {}
        self._queue = ReadyQueue()
        self._running = {}      # id -> JobRun currently RUNNING
        self._retrying = {}     # id -> JobRun waiting out a backoff
        self._now = 0

        for task in pipeline.tasks():
            run = JobRun(task)
            self._runs[task.id] = run
            self._machines[task.id] = StateMachine(run)

    # -- public API -------------------------------------------------------

    def run(self):
        """Execute the pipeline to completion and return a :class:`SchedulerResult`.

        Rejects a cyclic graph up front (a cycle can never make progress). Then
        it loops the four-phase tick until every run is settled, the loop makes
        no further progress (a resource or dependency deadlock), or the tick
        ceiling is hit.
        """

        self.dag.assert_acyclic()

        self._now = 0
        while not self._all_settled():
            if self._now > self.max_ticks:
                break
            progressed = self._tick()
            if not progressed and not self._running and not self._retrying:
                # Nothing ran, nothing is running, and nothing is waiting on a
                # backoff timer: no future tick can change anything. Stop rather
                # than spin. Any un-run tasks are left in their current state.
                break
            self._now += 1

        return SchedulerResult(
            runs=dict(self._runs),
            history=None,  # attached by the runner if a History is wired in
            bus=self.bus,
            resources=self.resources,
            ticks=self._now,
        )

    # -- the tick ---------------------------------------------------------

    def _tick(self):
        """Run one virtual tick. Returns ``True`` if anything changed."""

        progressed = False
        progressed = self._wake_retries() or progressed
        progressed = self._complete_running() or progressed
        progressed = self._unlock_ready() or progressed
        progressed = self._admit() or progressed
        return progressed

    def _wake_retries(self):
        """Return ``RETRYING`` runs whose backoff has elapsed to ``READY``."""

        progressed = False
        for task_id in list(self._retrying.keys()):
            run = self._retrying[task_id]
            if run.available_at <= self._now:
                del self._retrying[task_id]
                self._machines[task_id].advance(State.READY, self._now)
                self._emit_state(run)
                self._queue.requeue(task_id, run.priority)
                progressed = True
        return progressed

    def _complete_running(self):
        """Execute and settle every ``RUNNING`` run finishing at ``now``."""

        due = [
            run
            for run in self._running.values()
            if run.finish_at is not None and run.finish_at <= self._now
        ]
        # Deterministic completion order: by finish time, then insertion order.
        due.sort(key=lambda r: (r.finish_at, self.pipeline.task_ids().index(r.id)))

        progressed = False
        for run in due:
            self._complete_one(run)
            progressed = True
        return progressed

    def _complete_one(self, run):
        """Run one attempt of ``run`` and route it to its next state."""

        task = run.task
        del self._running[run.id]

        ctx = JobContext(run.id, run.attempts, self._now)
        failed = False
        error = None
        try:
            if task.action is not None:
                task.action(ctx)
        except Exception as exc:  # a failed attempt is an exception, by contract
            failed = True
            error = exc

        # Resources are released whether the attempt succeeded or failed.
        self._release_resources(run)

        machine = self._machines[run.id]
        if not failed:
            machine.advance(State.SUCCEEDED, self._now)
            self._emit_state(run)
            self._emit_terminal(run, "succeeded")
            return

        run.last_error = error
        if task.retry_policy.should_retry(run.attempts):
            machine.advance(State.RETRYING, self._now)
            self._emit_state(run)
            delay = task.retry_policy.next_delay(run.attempts)
            run.available_at = self._now + delay
            self._retrying[run.id] = run
        else:
            machine.advance(State.FAILED, self._now)
            self._emit_state(run)
            self._emit_terminal(run, "failed")
            self._cascade_skip(run)

    def _cascade_skip(self, failed_run):
        """Skip every task that transitively depends on ``failed_run``.

        When a task fails permanently none of its downstream tasks can ever run
        -- not just its direct children, but everything reachable from it. Each
        such task is moved to ``SKIPPED`` and removed from the ready queue so it
        is never dispatched.
        """

        for dep_id in self.dag.dependents(failed_run.id):
            run = self._runs[dep_id]
            if run.state.is_terminal():
                continue
            self._queue.remove(dep_id)
            self._retrying.pop(dep_id, None)
            self._running.pop(dep_id, None)
            self._machines[dep_id].advance(State.SKIPPED, self._now)
            self._emit_state(run)
            self._emit_terminal(run, "skipped")

    def _unlock_ready(self):
        """Move ``PENDING`` runs whose dependencies all succeeded to ``READY``."""

        progressed = False
        for task_id in self.pipeline.task_ids():
            run = self._runs[task_id]
            if run.state is not State.PENDING:
                continue
            if self._deps_satisfied(task_id):
                self._machines[task_id].advance(State.READY, self._now)
                self._emit_state(run)
                self._queue.push(task_id, run.priority)
                progressed = True
        return progressed

    def _deps_satisfied(self, task_id):
        """Return ``True`` if every dependency of ``task_id`` has ``SUCCEEDED``."""

        for dep in self.dag.dependencies(task_id):
            if self._runs[dep].state is not State.SUCCEEDED:
                return False
        return True

    def _admit(self):
        """Dispatch ready runs under the concurrency and resource limits."""

        progressed = False
        prev_priority = None
        while self._queue:
            if self.concurrency is not None and len(self._running) >= self.concurrency:
                break
            task_id = self._queue.peek()
            run = self._runs[task_id]
            costs = run.task.resource_costs

            if not self.resources.can_admit(costs):
                # Head-of-line blocking: the highest-priority ready task cannot
                # get its resources, so we wait rather than starting a lower
                # priority task ahead of it.
                break

            self._queue.pop()
            self.resources.try_admit(costs)
            self._emit_resource(run, "acquired")

            run.note_attempt()
            machine = self._machines[task_id]
            try:
                machine.advance(
                    State.RUNNING,
                    self._now,
                    batch_prev_priority=prev_priority,
                )
            except IllegalTransition:
                # The admission-batch ordering guard rejected this dispatch.
                # Undo the resource reservation and fail the run outright rather
                # than leaving the batch half-applied.
                self.resources.release(costs)
                self._emit_resource(run, "released")
                run.record_state(State.FAILED, self._now)
                self._emit_state(run)
                self._emit_terminal(run, "failed")
                break

            self._emit_state(run)
            self._emit_started(run)
            run.admitted_at = self._now
            run.finish_at = self._now + run.task.duration
            self._running[task_id] = run
            prev_priority = run.priority
            progressed = True
        return progressed

    # -- resources --------------------------------------------------------

    def _release_resources(self, run):
        costs = run.task.resource_costs
        if costs:
            self.resources.release(costs)
            self._emit_resource(run, "released")

    # -- settlement -------------------------------------------------------

    def _all_settled(self):
        """Return ``True`` when every run is terminal and nothing is in flight."""

        if self._running or self._retrying or self._queue:
            return False
        return all(run.state.is_terminal() for run in self._runs.values())

    # -- event narration --------------------------------------------------

    def _emit_state(self, run):
        self.bus.publish(
            "job.state",
            {"id": run.id, "state": run.state},
            at=self._now,
        )

    def _emit_started(self, run):
        self.bus.publish(
            "job.started",
            {"id": run.id, "attempt": run.attempts},
            at=self._now,
        )

    def _emit_terminal(self, run, outcome):
        self.bus.publish(
            "job.term.{}".format(outcome),
            {"id": run.id},
            at=self._now,
        )

    def _emit_resource(self, run, verb):
        for pool, cost in run.task.resource_costs.items():
            if cost > 0:
                self.bus.publish(
                    "resource.{}".format(verb),
                    {"pool": pool, "cost": cost, "id": run.id},
                    at=self._now,
                )

    def __repr__(self):
        return "Scheduler(pipeline={!r}, concurrency={})".format(
            self.pipeline.name, self.concurrency
        )
