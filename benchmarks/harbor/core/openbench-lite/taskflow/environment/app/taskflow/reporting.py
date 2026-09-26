"""Render runs, plans and graphs as human-readable text, tables and JSON.

The engine's objects have compact ``__repr__``s, but a person reading a run
wants something richer: an aligned table of every task's outcome, a summary
banner, a rendering of the dependency graph, or a machine-consumable JSON blob.
This module is the presentation layer that turns the model and result objects
into strings. It imports the read-only query surfaces (``RunReport``,
``History``, ``Dag``, ``ExecutionPlan``) and never mutates anything.

It leans on the standard library's ``json`` for the JSON forms and on
:mod:`taskflow.serialization` for the dict shapes those JSON forms wrap, so the
text and JSON views stay consistent. The table renderer is a small, dependency
free column formatter -- no third-party table package -- that pads columns to
their widest cell and draws a simple ASCII rule under the header.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from taskflow import serialization
from taskflow.dag import Dag
from taskflow.diagnostics import diagnose
from taskflow.model import State


def render_table(
    headers: Sequence[str], rows: Sequence[Sequence[Any]]
) -> str:
    """Render ``rows`` as a fixed-width text table under ``headers``.

    Each column is padded to the width of its widest cell (header included), cells
    are separated by two spaces, and a rule of dashes is drawn under the header
    row. Every cell is stringified with ``str``. An empty ``rows`` still renders
    the header and rule so the shape of the output is stable.
    """

    columns = len(headers)
    widths = [len(str(h)) for h in headers]
    string_rows: List[List[str]] = []
    for row in rows:
        cells = [str(cell) for cell in row]
        string_rows.append(cells)
        for i in range(columns):
            if i < len(cells) and len(cells[i]) > widths[i]:
                widths[i] = len(cells[i])

    def format_row(cells: Sequence[str]) -> str:
        padded = []
        for i in range(columns):
            cell = cells[i] if i < len(cells) else ""
            padded.append(cell.ljust(widths[i]))
        return "  ".join(padded).rstrip()

    lines = [format_row([str(h) for h in headers])]
    lines.append("  ".join("-" * width for width in widths).rstrip())
    for cells in string_rows:
        lines.append(format_row(cells))
    return "\n".join(lines)


def _state_symbol(state: State) -> str:
    """Return a short glyph-free marker for a state, for compact columns."""

    return {
        State.SUCCEEDED: "ok",
        State.FAILED: "FAIL",
        State.SKIPPED: "skip",
        State.CANCELLED: "cancel",
    }.get(state, str(state))


def render_run_table(report: Any) -> str:
    """Render a per-task outcome table for a :class:`~taskflow.runner.RunReport`.

    Columns: task id, final state, attempts, and the ticks the run occupied
    (derived from its timeline where available). Tasks appear in pipeline order.
    """

    headers = ["task", "state", "attempts", "outcome"]
    rows: List[List[Any]] = []
    for task_id, run in report.runs.items():
        rows.append(
            [
                task_id,
                str(run.state),
                run.attempts,
                _state_symbol(run.state),
            ]
        )
    return render_table(headers, rows)


def render_summary(report: Any) -> str:
    """Render a short multi-line banner summarising a run.

    Shows the pipeline name, whether it fully succeeded, the outcome counts, and
    the tick count, drawing the outcome partition from a
    :func:`taskflow.diagnostics.diagnose` pass so failures and skips are counted
    consistently with the diagnostics view.
    """

    diag = diagnose(report)
    lines = [
        "Pipeline: {}".format(report.pipeline.name),
        "Result:   {}".format("OK" if report.ok() else "INCOMPLETE"),
        "Tasks:    {} succeeded, {} failed, {} skipped".format(
            len(diag.succeeded), len(diag.failed), len(diag.skipped)
        ),
        "Ticks:    {}".format(report.ticks),
    ]
    return "\n".join(lines)


def render_report(report: Any) -> str:
    """Render a full text report: summary banner, a blank line, then the table."""

    return "{}\n\n{}".format(render_summary(report), render_run_table(report))


def report_to_dict(report: Any) -> Dict[str, Any]:
    """Return a JSON-friendly dict capturing a run's outcome.

    Bundles the pipeline name, overall success flag, tick count, the per-task
    final states and attempt counts, and the diagnostics partition. Every value
    is a JSON primitive so the result feeds straight into :func:`json.dumps`.
    """

    diag = diagnose(report)
    return {
        "pipeline": report.pipeline.name,
        "ok": report.ok(),
        "ticks": report.ticks,
        "states": {
            tid: serialization.state_to_str(state)
            for tid, state in report.states().items()
        },
        "attempts": {
            tid: run.attempts for tid, run in report.runs.items()
        },
        "diagnosis": diag.summary(),
    }


def render_json(report: Any, indent: int = 2) -> str:
    """Render a run as a JSON string via :func:`report_to_dict`."""

    return json.dumps(report_to_dict(report), indent=indent, sort_keys=True)


def render_dag(dag: Dag) -> str:
    """Render a dependency graph as an indented, level-by-level text tree.

    Each topological level is printed as a row, with each task followed by its
    direct dependents in parentheses, so the reader can see the wave structure
    and the fan-out at a glance. Roots are marked with a leading ``*``.
    """

    lines: List[str] = []
    roots = set(dag.roots())
    for index, level in enumerate(dag.topological_levels()):
        lines.append("level {}:".format(index))
        for node in level:
            marker = "*" if node in roots else " "
            dependents = dag.dependents(node)
            suffix = " -> {}".format(", ".join(dependents)) if dependents else ""
            lines.append("  {} {}{}".format(marker, node, suffix))
    return "\n".join(lines)


def render_dag_dot(dag: Dag, name: str = "pipeline") -> str:
    """Render a dependency graph in Graphviz DOT format.

    Produces a ``digraph`` with one edge per dependency, suitable for piping into
    Graphviz's ``dot`` for a picture. Node and edge order follow the graph's
    deterministic insertion order so the output is stable.
    """

    lines = ["digraph {} {{".format(_dot_id(name))]
    lines.append("  rankdir=LR;")
    for node in dag.nodes():
        lines.append("  {};".format(_dot_id(node)))
    for dependency, dependent in dag.edges():
        lines.append(
            "  {} -> {};".format(_dot_id(dependency), _dot_id(dependent))
        )
    lines.append("}")
    return "\n".join(lines)


def _dot_id(value: str) -> str:
    """Quote an identifier for DOT if it is not a bare word."""

    if value and all(c.isalnum() or c == "_" for c in value):
        return value
    return '"{}"'.format(value.replace('"', '\\"'))


def render_plan(plan: Any) -> str:
    """Render an :class:`~taskflow.planner.ExecutionPlan` as text.

    Shows the headline figures (depth, parallelism, both makespans, the critical
    path) followed by a timeline table of each task's simulated start and finish.
    """

    summary = plan.summary()
    header_lines = [
        "Plan: {}".format(summary["pipeline"]),
        "Tasks: {}  Depth: {}  Max parallelism: {}".format(
            summary["tasks"], summary["depth"], summary["max_parallelism"]
        ),
        "Ideal makespan: {}  Constrained makespan: {}".format(
            summary["ideal_makespan"], summary["constrained_makespan"]
        ),
        "Critical path: {}".format(" -> ".join(summary["critical_path"]) or "(none)"),
    ]
    rows = [
        [slot.task_id, slot.start, slot.finish, slot.duration]
        for slot in plan.timeline()
    ]
    table = render_table(["task", "start", "finish", "duration"], rows)
    return "{}\n\n{}".format("\n".join(header_lines), table)


def render_metrics(snapshot: Dict[str, Any]) -> str:
    """Render a :meth:`taskflow.metrics.MetricRegistry.snapshot` as text.

    Lays out each metric family (counters, gauges, histograms, timers) under a
    heading with one indented line per metric. Empty families are omitted so the
    output stays tight.
    """

    lines: List[str] = []
    counters = snapshot.get("counters", {})
    if counters:
        lines.append("counters:")
        for name in sorted(counters):
            lines.append("  {} = {}".format(name, counters[name]))
    gauges = snapshot.get("gauges", {})
    if gauges:
        lines.append("gauges:")
        for name in sorted(gauges):
            g = gauges[name]
            lines.append(
                "  {} = {} (peak {})".format(name, g["value"], g["peak"])
            )
    timers = snapshot.get("timers", {})
    if timers:
        lines.append("timers:")
        for name in sorted(timers):
            t = timers[name]
            lines.append(
                "  {} = {} span(s), mean {:.2f}".format(
                    name, t.get("count", 0), t.get("mean", 0.0)
                )
            )
    return "\n".join(lines)
