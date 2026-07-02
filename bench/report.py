#!/usr/bin/env python3
"""Statistical report for the agent-harness comparison benchmark.

Reads ``results/results.jsonl`` and prints a table with one row per harness:
per-task success (x/n), an overall success rate with a Wilson 95% confidence
interval, mean wall-clock time, and total reported tokens.

Python3 stdlib only.
"""

import argparse
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_RESULTS_PATH = os.path.join(REPO, "results", "results.jsonl")


def wilson_ci(successes, n, z=1.96):
    """Wilson score interval for a binomial proportion.

    Returns ``(lo, hi)`` clamped to [0, 1]. With no observations (``n == 0``)
    nothing is known, so the interval is the full ``(0.0, 1.0)``.

    Example: ``wilson_ci(4, 5)`` ~= ``(0.376, 0.964)``.
    """
    if n <= 0:
        return (0.0, 1.0)
    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = phat + z2 / (2.0 * n)
    margin = z * math.sqrt(phat * (1.0 - phat) / n + z2 / (4.0 * n * n))
    lo = (center - margin) / denom
    hi = (center + margin) / denom
    return (max(0.0, lo), min(1.0, hi))


def load_rows(results_path):
    """Load results rows from a JSONL file, skipping blank/corrupt lines."""
    rows = []
    if not os.path.isfile(results_path):
        return rows
    with open(results_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def aggregate(rows):
    """Aggregate rows into per-harness stats.

    Returns ``(harnesses, tasks, stats)`` where ``stats[harness]`` holds::

        {
          "per_task": {task: [successes, n]},
          "succ": int, "n": int,
          "wall_times": [float, ...],
          "tokens": int,          # sum of reported tokens
        }
    """
    harnesses = []
    tasks = []
    stats = {}
    for row in rows:
        harness = row.get("harness")
        task = row.get("task")
        if harness is None or task is None:
            continue
        if harness not in stats:
            stats[harness] = {"per_task": {}, "succ": 0, "n": 0,
                              "wall_times": [], "tokens": 0}
            harnesses.append(harness)
        if task not in tasks:
            tasks.append(task)

        st = stats[harness]
        pt = st["per_task"].setdefault(task, [0, 0])
        success = bool(row.get("success"))
        pt[1] += 1
        st["n"] += 1
        if success:
            pt[0] += 1
            st["succ"] += 1

        wt = row.get("wall_time_s")
        if isinstance(wt, (int, float)):
            st["wall_times"].append(wt)
        tok = row.get("tokens")
        if isinstance(tok, (int, float)):
            st["tokens"] += tok
    return harnesses, tasks, stats


def format_table(harnesses, tasks, stats):
    """Render the aggregated stats as a fixed-width text table."""
    headers = ["harness"] + tasks + ["overall", "wilson95", "mean_s", "tokens"]
    rows_text = []
    for harness in harnesses:
        st = stats[harness]
        cells = [harness]
        for task in tasks:
            succ, n = st["per_task"].get(task, [0, 0])
            cells.append(f"{succ}/{n}" if n else "-")
        n = st["n"]
        succ = st["succ"]
        rate = (succ / n) if n else 0.0
        lo, hi = wilson_ci(succ, n)
        cells.append(f"{succ}/{n} ({rate:.0%})" if n else "-")
        cells.append(f"[{lo:.3f}, {hi:.3f}]")
        mean_s = (sum(st["wall_times"]) / len(st["wall_times"])) \
            if st["wall_times"] else 0.0
        cells.append(f"{mean_s:.2f}")
        cells.append(str(st["tokens"]) if st["tokens"] else "-")
        rows_text.append(cells)

    widths = [len(h) for h in headers]
    for cells in rows_text:
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    lines = [fmt(headers), fmt(["-" * w for w in widths])]
    lines.extend(fmt(cells) for cells in rows_text)
    return "\n".join(lines)


def build_report(results_path):
    """Load, aggregate, and format a report from a results file."""
    rows = load_rows(results_path)
    if not rows:
        return f"No results found at {results_path}"
    harnesses, tasks, stats = aggregate(rows)
    return format_table(harnesses, tasks, stats)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Benchmark results report.")
    parser.add_argument("--results-path", default=DEFAULT_RESULTS_PATH,
                        help="override the results.jsonl path")
    args = parser.parse_args(argv)
    print(build_report(args.results_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
