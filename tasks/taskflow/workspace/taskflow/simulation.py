"""Scripted scenario simulation with deterministic fault injection.

Testing how a pipeline *responds* to failure normally means writing bespoke
action callables that fail on cue. This module makes that a data-driven exercise:
you describe outcomes -- "task ``t`` fails on its first two attempts then
succeeds", "task ``u`` always fails" -- as a :class:`Scenario`, and it synthesises
the action callables and runs the real engine against them.

Because the synthesised actions honour the attempt number handed to them in the
:class:`~taskflow.model.JobContext`, retries, cascade-skips and the whole
lifecycle behave exactly as they would in production; only the *decision* to fail
is scripted. That makes this a faithful way to explore resilience (does the
pipeline degrade the way you expect when a specific task is flaky?) without any
real work or real time.

The entry point is :func:`run_scenario`, which returns the ordinary
:class:`~taskflow.runner.RunReport` so all the existing reporting, diagnostics and
metrics tooling applies unchanged.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from taskflow.model import JobContext
from taskflow.runner import RunReport, run_pipeline


class TaskOutcome:
    """A scripted outcome for one task across its attempts.

    Parameters
    ----------
    fail_attempts:
        The 1-based attempt numbers on which the task should fail. An attempt not
        listed here succeeds. ``fail_attempts=[1, 2]`` fails the first two tries
        and succeeds on the third (if the retry policy allows it).
    always_fail:
        When ``True`` every attempt fails regardless of ``fail_attempts`` -- a
        shorthand for a permanently broken task.
    error_message:
        The message carried by the raised exception on a failing attempt.
    """

    __slots__ = ("fail_attempts", "always_fail", "error_message")

    def __init__(
        self,
        fail_attempts: Optional[List[int]] = None,
        always_fail: bool = False,
        error_message: str = "scripted failure",
    ) -> None:
        self.fail_attempts = set(fail_attempts or [])
        self.always_fail = always_fail
        self.error_message = error_message

    def should_fail(self, attempt: int) -> bool:
        """Return ``True`` if the attempt numbered ``attempt`` should fail."""

        if self.always_fail:
            return True
        return attempt in self.fail_attempts

    def __repr__(self) -> str:
        return "TaskOutcome(always_fail={}, fail_attempts={})".format(
            self.always_fail, sorted(self.fail_attempts)
        )


class Scenario:
    """A named collection of per-task scripted outcomes.

    Tasks without an explicit outcome succeed on their first attempt (the
    optimistic default). Build a scenario incrementally with :meth:`fails_on` and
    :meth:`always_fails`, then hand it to :func:`run_scenario`.
    """

    def __init__(self, name: str = "scenario") -> None:
        self.name = name
        self._outcomes: Dict[str, TaskOutcome] = {}

    def set_outcome(self, task_id: str, outcome: TaskOutcome) -> "Scenario":
        """Assign ``outcome`` to ``task_id`` and return ``self`` for chaining."""

        self._outcomes[task_id] = outcome
        return self

    def fails_on(
        self, task_id: str, attempts: List[int], message: str = "scripted failure"
    ) -> "Scenario":
        """Script ``task_id`` to fail on the given 1-based attempt numbers."""

        return self.set_outcome(
            task_id, TaskOutcome(fail_attempts=attempts, error_message=message)
        )

    def always_fails(
        self, task_id: str, message: str = "scripted failure"
    ) -> "Scenario":
        """Script ``task_id`` to fail on every attempt."""

        return self.set_outcome(
            task_id, TaskOutcome(always_fail=True, error_message=message)
        )

    def outcome_for(self, task_id: str) -> Optional[TaskOutcome]:
        """Return the scripted outcome for ``task_id``, or ``None`` if default."""

        return self._outcomes.get(task_id)

    def scripted_tasks(self) -> List[str]:
        """Return the ids of tasks that have a non-default scripted outcome."""

        return list(self._outcomes.keys())

    def __repr__(self) -> str:
        return "Scenario(name={!r}, scripted={})".format(
            self.name, len(self._outcomes)
        )


def _make_action(outcome: Optional[TaskOutcome]) -> Callable[[JobContext], None]:
    """Return an action callable enacting ``outcome`` (or an always-succeed no-op).

    The returned callable inspects the attempt number on the
    :class:`~taskflow.model.JobContext` it is given. The scheduler increments the
    run's attempt counter *before* the action runs and passes the pre-increment
    value as ``ctx.attempt``, so attempt ``n`` arrives as ``ctx.attempt == n``.
    """

    if outcome is None:
        def succeed(_ctx: JobContext) -> None:
            return None

        return succeed

    def scripted(ctx: JobContext) -> None:
        # ctx.attempt is 1-based for the attempt about to be evaluated.
        if outcome.should_fail(ctx.attempt):
            raise RuntimeError(outcome.error_message)
        return None

    return scripted


def build_scenario_config(
    base_config: Dict[str, Any], scenario: Scenario
) -> Dict[str, Any]:
    """Return a copy of ``base_config`` with scripted actions attached.

    Every task's ``action`` is replaced by a synthesised callable enacting the
    scenario's outcome for that task (or an always-succeed no-op if the scenario
    does not mention it). The original config is not mutated; a shallow-plus-tasks
    copy is returned so the caller keeps their template intact.
    """

    tasks: List[Dict[str, Any]] = []
    for task in base_config.get("tasks", []):
        spec = dict(task)
        spec["action"] = _make_action(scenario.outcome_for(spec.get("id")))
        tasks.append(spec)
    new_config = dict(base_config)
    new_config["tasks"] = tasks
    return new_config


def run_scenario(
    base_config: Dict[str, Any],
    scenario: Scenario,
    concurrency: Optional[int] = None,
    max_ticks: Optional[int] = None,
) -> RunReport:
    """Run ``base_config`` with ``scenario``'s scripted outcomes and report.

    Synthesises fault-injecting actions from the scenario, then drives the real
    :func:`taskflow.runner.run_pipeline`, so the returned
    :class:`~taskflow.runner.RunReport` reflects genuine engine behaviour under
    the scripted faults -- retries, cascade skips and all. The base config's task
    ``action`` entries (if any) are ignored in favour of the scripted ones.
    """

    config = build_scenario_config(base_config, scenario)
    return run_pipeline(config, concurrency=concurrency, max_ticks=max_ticks)


def compare_scenarios(
    base_config: Dict[str, Any], scenarios: List[Scenario]
) -> Dict[str, Dict[str, str]]:
    """Run several scenarios against one config and tabulate the final states.

    Returns ``{scenario_name: {task_id: state_string}}`` so a caller can see, at
    a glance, how differently the pipeline settles under each fault scenario.
    Useful for regression-style resilience checks.
    """

    results: Dict[str, Dict[str, str]] = {}
    for scenario in scenarios:
        report = run_scenario(base_config, scenario)
        results[scenario.name] = {
            tid: str(state) for tid, state in report.states().items()
        }
    return results
