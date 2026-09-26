"""Export runs, plans and graphs to portable text formats.

Where :mod:`taskflow.reporting` renders things for a human reading a terminal,
this module renders them for *other* tools: a CSV of run outcomes for a
spreadsheet, a Mermaid flowchart for a wiki, or an ASCII Gantt chart of a
planned schedule. The formats are all plain text and standard-library only, so
nothing here needs a rendering dependency.

Each exporter is a pure function from a model/result object (plus a couple of
formatting options) to a string. They read only the public query surfaces and
never mutate their inputs.
"""

from __future__ import annotations

import csv
import io
from typing import Any, Dict, List, Optional

from taskflow.dag import Dag


def run_to_csv(report: Any) -> str:
    """Export a run's per-task outcomes as CSV text.

    Columns: ``task,state,attempts,retried``. Rows are in pipeline order. Uses
    the standard library's :mod:`csv` writer so quoting is handled correctly for
    any exotic task ids.
    """

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["task", "state", "attempts", "retried"])
    for task_id, run in report.runs.items():
        writer.writerow(
            [
                task_id,
                str(run.state),
                run.attempts,
                "yes" if run.attempts > 1 else "no",
            ]
        )
    return buffer.getvalue()


def dag_to_mermaid(dag: Dag, direction: str = "LR") -> str:
    """Export a dependency graph as a Mermaid ``flowchart``.

    ``direction`` is a Mermaid layout hint (``"LR"`` left-to-right, ``"TD"``
    top-down). Each dependency becomes an arrow ``dep --> task``. Node ids are
    sanitised into Mermaid-safe identifiers with the original id shown as the node
    label, so ids containing dots or dashes still render.
    """

    lines = ["flowchart {}".format(direction)]
    for node in dag.nodes():
        lines.append("    {}[{}]".format(_mermaid_id(node), node))
    for dependency, dependent in dag.edges():
        lines.append(
            "    {} --> {}".format(
                _mermaid_id(dependency), _mermaid_id(dependent)
            )
        )
    return "\n".join(lines)


def _mermaid_id(value: str) -> str:
    """Return a Mermaid-safe node identifier derived from ``value``."""

    safe = "".join(c if c.isalnum() else "_" for c in value)
    if not safe or not (safe[0].isalpha() or safe[0] == "_"):
        safe = "n_" + safe
    return safe


def plan_to_gantt(plan: Any, width: int = 40) -> str:
    """Export an :class:`~taskflow.planner.ExecutionPlan` as an ASCII Gantt chart.

    Each task becomes a row whose bar spans its simulated start-to-finish window,
    scaled so the whole makespan fits in ``width`` characters. A task on the
    critical path is drawn with ``#`` and others with ``=``, so the critical
    chain stands out. Rows are ordered by start time.
    """

    makespan = plan.constrained_makespan or 1
    critical = set(plan.critical)
    label_width = max((len(s.task_id) for s in plan.slots.values()), default=4)
    lines: List[str] = []
    for slot in plan.timeline():
        start_col = int(slot.start / makespan * width)
        end_col = int(slot.finish / makespan * width)
        bar_char = "#" if slot.task_id in critical else "="
        length = max(end_col - start_col, 1)
        bar = " " * start_col + bar_char * length
        bar = bar.ljust(width)
        lines.append(
            "{}  |{}| {}-{}".format(
                slot.task_id.ljust(label_width), bar, slot.start, slot.finish
            )
        )
    return "\n".join(lines)


def pipeline_to_edge_list(dag: Dag) -> str:
    """Export a graph as a newline-separated ``dependency -> dependent`` list.

    The simplest possible interchange format: one edge per line. Isolated nodes
    (no edges) are emitted on their own line so they are not lost.
    """

    lines: List[str] = []
    connected = set()
    for dependency, dependent in dag.edges():
        lines.append("{} -> {}".format(dependency, dependent))
        connected.add(dependency)
        connected.add(dependent)
    for node in dag.nodes():
        if node not in connected:
            lines.append(node)
    return "\n".join(lines)


def metrics_to_csv(snapshot: Dict[str, Any]) -> str:
    """Export a metrics snapshot's scalar values as ``family,name,value`` CSV.

    Counters and gauges (their current value) are flattened into rows; histogram
    and timer summaries contribute their mean and count. Handy for dropping a
    run's metrics into a spreadsheet for comparison across runs.
    """

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["family", "name", "value"])
    for name, value in snapshot.get("counters", {}).items():
        writer.writerow(["counter", name, value])
    for name, gauge in snapshot.get("gauges", {}).items():
        writer.writerow(["gauge", name, gauge["value"]])
        writer.writerow(["gauge_peak", name, gauge["peak"]])
    for name, summary in snapshot.get("histograms", {}).items():
        writer.writerow(["histogram_mean", name, summary.get("mean", 0.0)])
    for name, summary in snapshot.get("timers", {}).items():
        writer.writerow(["timer_mean", name, summary.get("mean", 0.0)])
    return buffer.getvalue()
