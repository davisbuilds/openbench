"""Dry-run execution planning: what a pipeline *would* do before it runs.

Running a pipeline tells you what happened; a plan tells you what to expect.
This module analyses a :class:`~taskflow.model.Pipeline` (plus optional
concurrency and resource limits) *without executing any task action* and
produces an :class:`ExecutionPlan`: the wave-by-wave shape of the schedule, the
critical path, and an estimated makespan in virtual ticks.

Two estimates matter:

* **Ideal makespan** -- the critical-path length using each task's ``duration``
  as its weight. This is the floor: no schedule can finish sooner, no matter how
  much concurrency you throw at it.
* **Constrained makespan** -- the result of a deterministic list-scheduling
  simulation that respects the concurrency limit and resource-pool capacities,
  dispatching ready tasks highest-priority-first exactly as the real scheduler
  would. This assumes every task succeeds on its first attempt (no retries, no
  skips), so it is a best-case-under-constraints figure, not a prediction of a
  flaky run.

Because it never runs actions, the planner is cheap and side-effect free, which
makes it suitable for CLIs, capacity sizing ("would a concurrency of 3 help?"),
and pre-flight sanity checks.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from taskflow.dag import Dag
from taskflow.dag_algorithms import critical_path, level_widths
from taskflow.model import Pipeline


class ScheduledSlot:
    """One task's placement in a simulated schedule.

    Parameters
    ----------
    task_id:
        The task placed.
    start:
        The virtual tick at which it would begin running.
    finish:
        The virtual tick at which it would complete (``start + duration``).
    """

    __slots__ = ("task_id", "start", "finish")

    def __init__(self, task_id: str, start: int, finish: int) -> None:
        self.task_id = task_id
        self.start = start
        self.finish = finish

    @property
    def duration(self) -> int:
        """The number of ticks the slot occupies."""

        return self.finish - self.start

    def __repr__(self) -> str:
        return "ScheduledSlot({!r}, {}->{})".format(
            self.task_id, self.start, self.finish
        )


class ExecutionPlan:
    """A computed, non-executing forecast of how a pipeline would run.

    Bundles the structural waves, the critical path, the per-task simulated
    placement, and the two makespan estimates so a caller has one object to
    query and render.
    """

    def __init__(
        self,
        pipeline_name: str,
        levels: List[List[str]],
        critical: List[str],
        ideal_makespan: int,
        constrained_makespan: int,
        slots: Dict[str, ScheduledSlot],
        concurrency: Optional[int],
    ) -> None:
        self.pipeline_name = pipeline_name
        self.levels = levels
        self.critical = critical
        self.ideal_makespan = ideal_makespan
        self.constrained_makespan = constrained_makespan
        self.slots = slots
        self.concurrency = concurrency

    def depth(self) -> int:
        """Return the number of dependency waves (levels)."""

        return len(self.levels)

    def max_parallelism(self) -> int:
        """Return the widest wave -- the most tasks eligible at once."""

        return max((len(level) for level in self.levels), default=0)

    def peak_concurrency(self) -> int:
        """Return the most tasks the simulation ran simultaneously.

        Computed from the slot start/finish spans, so it reflects the *actual*
        overlap under the constraints, which the concurrency limit may hold below
        :meth:`max_parallelism`.
        """

        events: List[Tuple[int, int]] = []
        for slot in self.slots.values():
            events.append((slot.start, 1))
            events.append((slot.finish, -1))
        events.sort()
        current = 0
        peak = 0
        for _time, delta in events:
            current += delta
            if current > peak:
                peak = current
        return peak

    def slack_of(self, task_id: str) -> int:
        """Return how many ticks ``task_id`` could slip without extending the run.

        The slack (float) of a task is the difference between its latest possible
        finish and its simulated finish. A task on the critical path has zero
        slack; a task with positive slack has scheduling freedom. Computed simply
        here as ``constrained_makespan - simulated_finish`` for tasks off the
        critical path, and ``0`` for those on it.
        """

        if task_id in self.critical:
            return 0
        slot = self.slots.get(task_id)
        if slot is None:
            return 0
        return max(self.constrained_makespan - slot.finish, 0)

    def timeline(self) -> List[ScheduledSlot]:
        """Return every slot ordered by start time, then finish, then id."""

        return sorted(
            self.slots.values(),
            key=lambda s: (s.start, s.finish, s.task_id),
        )

    def summary(self) -> Dict[str, object]:
        """Return a compact dict of the plan's headline figures."""

        return {
            "pipeline": self.pipeline_name,
            "tasks": len(self.slots),
            "depth": self.depth(),
            "max_parallelism": self.max_parallelism(),
            "peak_concurrency": self.peak_concurrency(),
            "ideal_makespan": self.ideal_makespan,
            "constrained_makespan": self.constrained_makespan,
            "critical_path": list(self.critical),
        }

    def __repr__(self) -> str:
        return "ExecutionPlan(name={!r}, makespan={}, critical={})".format(
            self.pipeline_name, self.constrained_makespan, self.critical
        )


def _durations(pipeline: Pipeline) -> Dict[str, int]:
    """Return a ``{task_id: duration}`` map for the pipeline."""

    return {task.id: task.duration for task in pipeline.tasks()}


def _priorities(pipeline: Pipeline) -> Dict[str, int]:
    """Return a ``{task_id: priority}`` map for the pipeline."""

    return {task.id: task.priority for task in pipeline.tasks()}


def _simulate(
    pipeline: Pipeline,
    dag: Dag,
    concurrency: Optional[int],
    pools: Optional[Dict[str, int]],
) -> Tuple[Dict[str, ScheduledSlot], int]:
    """List-schedule the pipeline under the given limits; no actions run.

    Advances a virtual clock, at each tick admitting ready tasks (all
    dependencies finished) highest-priority-first while the concurrency limit and
    resource pools allow, then jumping to the next completion. Every task is
    assumed to succeed on its first attempt. Returns the per-task slots and the
    constrained makespan.
    """

    durations = _durations(pipeline)
    priorities = _priorities(pipeline)
    costs = {
        task.id: dict(task.resource_costs) for task in pipeline.tasks()
    }
    capacities = dict(pools) if pools else {}
    used = {name: 0 for name in capacities}

    finished: Dict[str, int] = {}       # task_id -> finish tick
    slots: Dict[str, ScheduledSlot] = {}
    running: Dict[str, int] = {}        # task_id -> finish tick
    remaining = set(pipeline.task_ids())
    now = 0

    def can_admit(task_id: str) -> bool:
        for pool, cost in costs[task_id].items():
            if cost <= 0:
                continue
            cap = capacities.get(pool)
            if cap is None:
                continue
            if used.get(pool, 0) + cost > cap:
                return False
        return True

    def deps_done(task_id: str) -> bool:
        return all(dep in finished for dep in dag.dependencies(task_id))

    guard = 0
    max_guard = 10 * (len(pipeline) + 1) + sum(durations.values()) + 1
    while remaining or running:
        guard += 1
        if guard > max_guard:
            break

        # Admit as many ready tasks as the limits permit, priority-first.
        ready = [
            tid
            for tid in pipeline.task_ids()
            if tid in remaining and tid not in running and deps_done(tid)
        ]
        ready.sort(key=lambda tid: (-priorities[tid], pipeline.task_ids().index(tid)))
        for tid in ready:
            if concurrency is not None and len(running) >= concurrency:
                break
            if not can_admit(tid):
                continue
            for pool, cost in costs[tid].items():
                if cost > 0 and pool in used:
                    used[pool] += cost
                elif cost > 0 and pool in capacities:
                    used[pool] = used.get(pool, 0) + cost
            finish = now + durations[tid]
            slots[tid] = ScheduledSlot(tid, now, finish)
            running[tid] = finish
            remaining.discard(tid)

        if not running:
            # Nothing running and nothing admissible: either done or deadlocked.
            if not remaining:
                break
            # Deadlock (resources can never free up); stop advancing.
            break

        # Jump to the next completion time.
        next_finish = min(running.values())
        now = next_finish
        for tid in [t for t, f in running.items() if f <= now]:
            finished[tid] = running.pop(tid)
            for pool, cost in costs[tid].items():
                if cost > 0 and pool in used:
                    used[pool] -= cost

    makespan = max(finished.values(), default=0)
    return slots, makespan


def plan_pipeline(
    pipeline: Pipeline,
    concurrency: Optional[int] = None,
    pools: Optional[Dict[str, int]] = None,
    dag: Optional[Dag] = None,
) -> ExecutionPlan:
    """Compute an :class:`ExecutionPlan` for ``pipeline`` without running it.

    Parameters
    ----------
    pipeline:
        The pipeline to analyse.
    concurrency:
        The concurrency limit to simulate under. ``None`` means unlimited (only
        resource pools and dependencies constrain parallelism).
    pools:
        A ``{pool: capacity}`` mapping to bound resource-limited concurrency,
        mirroring the runner's ``pools`` block. ``None`` means unbounded pools.
    dag:
        A prebuilt dependency graph; one is derived from the pipeline if omitted.

    The plan reports both the ideal (critical-path) makespan and the constrained
    makespan from the list-scheduling simulation.
    """

    graph = dag if dag is not None else Dag.from_pipeline(pipeline)
    graph.assert_acyclic()

    levels = graph.topological_levels() if len(graph) else []
    durations = _durations(pipeline)
    critical, ideal = critical_path(graph, durations)
    slots, constrained = _simulate(pipeline, graph, concurrency, pools)

    return ExecutionPlan(
        pipeline_name=pipeline.name,
        levels=levels,
        critical=critical,
        ideal_makespan=ideal,
        constrained_makespan=constrained,
        slots=slots,
        concurrency=concurrency,
    )


def estimate_makespan(
    pipeline: Pipeline,
    concurrency: Optional[int] = None,
    pools: Optional[Dict[str, int]] = None,
) -> int:
    """Return just the constrained makespan estimate for ``pipeline``.

    A convenience wrapper over :func:`plan_pipeline` for callers that only want
    the headline number.
    """

    return plan_pipeline(
        pipeline, concurrency=concurrency, pools=pools
    ).constrained_makespan


def parallelism_profile(pipeline: Pipeline) -> List[int]:
    """Return the width of each dependency wave, in order.

    A quick, simulation-free view of how much parallelism the pipeline *offers*
    at each depth (ignoring concurrency and resource limits). The peak of this
    profile is the most tasks that could ever run at once.
    """

    dag = Dag.from_pipeline(pipeline)
    return level_widths(dag) if len(dag) else []
