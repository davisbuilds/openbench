"""Explain *why* a run turned out the way it did.

A :class:`~taskflow.runner.RunReport` tells you the outcome; diagnostics tell you
the story behind it. When a pipeline does not fully succeed, the useful questions
are: which task actually *failed* (as opposed to being skipped in the fallout),
which tasks were skipped and because of what upstream failure, and how much retry
churn happened along the way. This module answers those by cross-referencing the
final run states with the dependency graph.

The centrepiece is :func:`diagnose`, which produces a :class:`Diagnosis`
bundling:

* the terminal-outcome partition (succeeded / failed / skipped),
* for every skipped task, the chain of failed ancestors that doomed it
  (:func:`skip_reason`), and
* a retry summary (which tasks retried and how many times).

Everything is derived from the report's runs and a dependency graph; nothing is
executed. The analysis is deterministic and read-only.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from taskflow.dag import Dag
from taskflow.model import State


class Diagnosis:
    """A structured explanation of a finished run.

    Holds the outcome partition plus the derived skip reasons and retry summary
    so a caller (or :mod:`taskflow.reporting`) can render a "why did this run end
    like this" narrative without re-deriving anything.
    """

    def __init__(
        self,
        succeeded: List[str],
        failed: List[str],
        skipped: List[str],
        skip_reasons: Dict[str, List[str]],
        retries: Dict[str, int],
    ) -> None:
        self.succeeded = succeeded
        self.failed = failed
        self.skipped = skipped
        self.skip_reasons = skip_reasons
        self.retries = retries

    def ok(self) -> bool:
        """Return ``True`` if nothing failed or was skipped."""

        return not self.failed and not self.skipped

    def root_failures(self) -> List[str]:
        """Return the tasks that failed on their own (the true root causes).

        Every failed task is a root failure here: a task only reaches ``FAILED``
        by exhausting its own retries, never by fallout (fallout produces
        ``SKIPPED``). This is exposed as a named query because it is the set a
        post-mortem should focus on.
        """

        return list(self.failed)

    def blast_radius(self) -> int:
        """Return how many tasks were skipped as a consequence of failures."""

        return len(self.skipped)

    def total_retries(self) -> int:
        """Return the total number of retries across every task.

        A task that made ``n`` attempts contributes ``n - 1`` retries; the sum is
        the run's total retry churn.
        """

        return sum(max(count - 1, 0) for count in self.retries.values())

    def summary(self) -> Dict[str, object]:
        """Return a compact dict summary of the diagnosis."""

        return {
            "ok": self.ok(),
            "succeeded": len(self.succeeded),
            "failed": self.failed,
            "skipped": self.skipped,
            "root_failures": self.root_failures(),
            "total_retries": self.total_retries(),
        }

    def __repr__(self) -> str:
        return "Diagnosis(ok={}, failed={}, skipped={})".format(
            self.ok(), self.failed, self.skipped
        )


def _states(report: object) -> Dict[str, State]:
    """Return the ``{task_id: State}`` map from a report-like object.

    Accepts anything exposing a ``states()`` method (a
    :class:`~taskflow.runner.RunReport` or a
    :class:`~taskflow.scheduler.SchedulerResult`), keeping the diagnostics usable
    against either.
    """

    return report.states()


def skip_reason(
    task_id: str, states: Dict[str, State], dag: Dag
) -> List[str]:
    """Return the failed ancestors of ``task_id`` that caused it to be skipped.

    Walks upstream from ``task_id`` and collects every ancestor that ended in
    ``FAILED``. Those are the root causes whose permanent failure cascaded down
    to skip this task. The list is ordered from nearest to furthest ancestor. An
    empty list means the task was not skipped due to an upstream failure (it may
    not have been skipped at all).
    """

    if task_id not in dag:
        return []
    reasons: List[str] = []
    for ancestor in dag.transitive_dependencies(task_id):
        if states.get(ancestor) is State.FAILED:
            reasons.append(ancestor)
    return reasons


def diagnose(report: object, dag: Optional[Dag] = None) -> Diagnosis:
    """Produce a :class:`Diagnosis` for a finished run.

    Parameters
    ----------
    report:
        A :class:`~taskflow.runner.RunReport` (or any object exposing
        ``states()`` and ``runs``). The dependency graph is taken from
        ``report.pipeline`` when ``dag`` is not supplied.
    dag:
        The dependency graph to explain skips against; derived from the report's
        pipeline if omitted.

    The returned diagnosis partitions the tasks by outcome, computes the skip
    reason chain for every skipped task, and tallies retries from the run
    records.
    """

    states = _states(report)
    if dag is None:
        pipeline = getattr(report, "pipeline", None)
        if pipeline is not None:
            dag = Dag.from_pipeline(pipeline)
        else:
            dag = Dag()

    succeeded = [tid for tid, s in states.items() if s is State.SUCCEEDED]
    failed = [tid for tid, s in states.items() if s is State.FAILED]
    skipped = [tid for tid, s in states.items() if s is State.SKIPPED]

    skip_reasons = {
        tid: skip_reason(tid, states, dag) for tid in skipped
    }

    retries: Dict[str, int] = {}
    runs = getattr(report, "runs", {})
    for tid, run in runs.items():
        attempts = getattr(run, "attempts", 0)
        if attempts > 1:
            retries[tid] = attempts

    return Diagnosis(
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        skip_reasons=skip_reasons,
        retries=retries,
    )


def explain(report: object, dag: Optional[Dag] = None) -> List[str]:
    """Return a list of human-readable lines explaining a run's outcome.

    A convenience over :func:`diagnose` that renders the diagnosis as narrative
    lines suitable for logging or a CLI: one line per failure and per skip (with
    its cause chain), plus a retry note. A fully successful run yields a single
    reassuring line.
    """

    diag = diagnose(report, dag=dag)
    if diag.ok():
        return ["All {} task(s) succeeded.".format(len(diag.succeeded))]

    lines: List[str] = []
    for tid in diag.failed:
        attempts = diag.retries.get(tid, 1)
        lines.append(
            "FAILED: {!r} gave up after {} attempt(s).".format(tid, attempts)
        )
    for tid in diag.skipped:
        causes = diag.skip_reasons.get(tid, [])
        if causes:
            lines.append(
                "SKIPPED: {!r} was skipped because {} failed.".format(
                    tid, ", ".join(repr(c) for c in causes)
                )
            )
        else:
            lines.append("SKIPPED: {!r} was skipped.".format(tid))
    if diag.total_retries():
        lines.append(
            "Retries: {} retry attempt(s) across {} task(s).".format(
                diag.total_retries(), len(diag.retries)
            )
        )
    return lines
