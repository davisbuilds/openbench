"""Static analysis and linting for pipeline structure.

Before a pipeline ever runs there are structural smells worth flagging: a task
no path can ever reach, a redundant dependency that the graph already implies
transitively, a resource demand that exceeds the pool that must satisfy it, or a
retry configuration that can never actually retry. This module inspects a loaded
:class:`~taskflow.model.Pipeline` (and its :class:`~taskflow.dag.Dag`) and reports
those findings, mirroring the severity model of :mod:`taskflow.validation` but
operating on the *built* pipeline rather than the raw config dict.

The findings are advisory: a pipeline can run with all of them present. They are
the kind of thing a ``lint`` step surfaces so an author can tidy a definition. As
with the rest of the analysis layer, nothing here executes a task or mutates the
pipeline; it only reads and reports.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from taskflow.dag import Dag
from taskflow.dag_algorithms import connected_components
from taskflow.model import Pipeline


class Finding:
    """One structural observation about a pipeline.

    Parameters
    ----------
    code:
        A short stable identifier for the kind of finding, e.g.
        ``"redundant-dep"`` or ``"resource-oversubscribed"``.
    location:
        The task id (or ``"<pipeline>"``) the finding concerns.
    message:
        A human-readable description.
    severity:
        ``"warning"`` (default) or ``"info"``.
    """

    __slots__ = ("code", "location", "message", "severity")

    def __init__(
        self,
        code: str,
        location: str,
        message: str,
        severity: str = "warning",
    ) -> None:
        self.code = code
        self.location = location
        self.message = message
        self.severity = severity

    def __repr__(self) -> str:
        return "{}[{}] {}: {}".format(
            self.severity.upper(), self.code, self.location, self.message
        )


def find_isolated_tasks(pipeline: Pipeline, dag: Dag) -> List[Finding]:
    """Flag tasks with neither dependencies nor dependents.

    An isolated task runs, but its presence in a pipeline of otherwise connected
    work is often an oversight (a forgotten wiring). Only reported when the
    pipeline has more than one task, since a single-task pipeline is trivially
    "isolated" by design.
    """

    if len(pipeline) <= 1:
        return []
    findings: List[Finding] = []
    for task_id in dag.nodes():
        if not dag.dependencies(task_id) and not dag.dependents(task_id):
            findings.append(
                Finding(
                    "isolated-task",
                    task_id,
                    "task has no dependencies and no dependents",
                    severity="info",
                )
            )
    return findings


def find_redundant_dependencies(pipeline: Pipeline, dag: Dag) -> List[Finding]:
    """Flag direct dependencies already implied transitively.

    If ``c`` depends on both ``a`` and ``b``, and ``b`` itself depends on ``a``,
    then ``c``'s direct edge to ``a`` is redundant: ``a`` is guaranteed to finish
    before ``b``, hence before ``c``. Removing it does not change the schedule
    and makes the intent clearer.
    """

    findings: List[Finding] = []
    for task_id in dag.nodes():
        direct = dag.dependencies(task_id)
        direct_set = set(direct)
        for dep in direct:
            # Ancestors of dep that are also listed as direct deps are redundant.
            for ancestor in dag.transitive_dependencies(dep):
                if ancestor in direct_set:
                    findings.append(
                        Finding(
                            "redundant-dep",
                            task_id,
                            "dependency {!r} is implied via {!r}".format(
                                ancestor, dep
                            ),
                        )
                    )
    return findings


def find_resource_oversubscription(
    pipeline: Pipeline, pools: Optional[Dict[str, int]]
) -> List[Finding]:
    """Flag tasks demanding more of a pool than the pool can ever supply.

    If a single task charges 4 units to a pool with capacity 2, that task can
    never be admitted and the pipeline will stall on it forever. This is a
    genuine dead-on-arrival configuration, so it is reported as a warning. Pools
    absent from ``pools`` are treated as unbounded and never oversubscribed.
    """

    if not pools:
        return []
    findings: List[Finding] = []
    for task in pipeline.tasks():
        for pool, cost in task.resource_costs.items():
            capacity = pools.get(pool)
            if capacity is not None and cost > capacity:
                findings.append(
                    Finding(
                        "resource-oversubscribed",
                        task.id,
                        "charges {} to pool {!r} of capacity {}; can never be "
                        "admitted".format(cost, pool, capacity),
                    )
                )
    return findings


def find_useless_retries(pipeline: Pipeline) -> List[Finding]:
    """Flag retry policies that cannot actually retry.

    A policy with ``max_attempts == 1`` never retries -- its first failure is
    permanent. Configuring a backoff on such a policy is a no-op and usually
    signals a mistaken expectation, so it is worth an info-level nudge.
    """

    findings: List[Finding] = []
    for task in pipeline.tasks():
        policy = task.retry_policy
        if policy.max_attempts == 1 and policy.base > 0:
            findings.append(
                Finding(
                    "useless-backoff",
                    task.id,
                    "backoff base {} set but max_attempts is 1 (never "
                    "retries)".format(policy.base),
                    severity="info",
                )
            )
    return findings


def find_disconnected_components(pipeline: Pipeline, dag: Dag) -> List[Finding]:
    """Flag a pipeline that splits into several unrelated sub-graphs.

    Multiple weakly-connected components mean the pipeline is really several
    independent pipelines sharing a name. That is legal and sometimes intended,
    but flagging it helps an author notice an accidental split (a typo'd
    dependency severing the graph).
    """

    components = connected_components(dag)
    if len(components) <= 1:
        return []
    return [
        Finding(
            "disconnected",
            "<pipeline>",
            "pipeline has {} independent components: {}".format(
                len(components),
                "; ".join("[" + ", ".join(c) + "]" for c in components),
            ),
            severity="info",
        )
    ]


def analyze(
    pipeline: Pipeline,
    pools: Optional[Dict[str, int]] = None,
    dag: Optional[Dag] = None,
) -> List[Finding]:
    """Run every structural check and return all findings, most-severe first.

    Bundles the individual ``find_*`` passes into one call. ``pools`` enables the
    resource-oversubscription check; ``dag`` is derived from the pipeline if not
    supplied. Findings are returned sorted so warnings precede info-level notes,
    and within a severity in the order the passes ran.
    """

    graph = dag if dag is not None else Dag.from_pipeline(pipeline)
    findings: List[Finding] = []
    findings.extend(find_isolated_tasks(pipeline, graph))
    findings.extend(find_redundant_dependencies(pipeline, graph))
    findings.extend(find_resource_oversubscription(pipeline, pools))
    findings.extend(find_useless_retries(pipeline))
    findings.extend(find_disconnected_components(pipeline, graph))

    severity_rank = {"warning": 0, "info": 1}
    findings.sort(key=lambda f: severity_rank.get(f.severity, 2))
    return findings


def lint_summary(findings: List[Finding]) -> Dict[str, int]:
    """Return a ``{code: count}`` tally of findings for a quick overview."""

    summary: Dict[str, int] = {}
    for finding in findings:
        summary[finding.code] = summary.get(finding.code, 0) + 1
    return summary
