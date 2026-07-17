#!/usr/bin/env python3
"""Build a matched-denominator scorecard from OpenBench result JSONL files.

Each input file is an arm when multiple paths are supplied.  With one input,
rows are split by candidate name (when present) or harness.  Only unique
(task, trial) cells present in every arm contribute to the scorecard.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

try:  # Package import path.
    from . import stats
except ImportError:  # Script path (`python3 bench/compare.py`).
    import stats


TOKEN_METRICS = (
    ("Uncached input tokens / solve", "tokens_input_uncached"),
    ("Cache-read tokens / solve", "tokens_cache_read"),
    ("Output tokens / solve", "tokens_output"),
)


def _path_labels(paths):
    """Return stable, unique labels derived from input filenames."""
    used = Counter()
    labels = []
    for path in paths:
        base = os.path.basename(path)
        label = os.path.splitext(base)[0] or base
        used[label] += 1
        labels.append(label if used[label] == 1 else f"{label}-{used[label]}")
    return labels


def _row_arm(row):
    provenance = row.get("candidate_provenance")
    if isinstance(provenance, dict) and provenance.get("name"):
        return str(provenance["name"])
    return str(row.get("harness") or "-")


def load_arms(paths):
    """Load paths and return ``{arm: rows}`` according to CLI arm semantics."""
    if len(paths) > 1:
        arms = defaultdict(list)
        for path, label in zip(paths, _path_labels(paths)):
            arms[label].extend(stats.load_rows([path]))
        return dict(arms)

    arms = defaultdict(list)
    for row in stats.load_rows(paths):
        arms[_row_arm(row)].append(row)
    return dict(arms)


def _filter_arm(rows, tasks_dirs):
    filtered = stats.filter_rows(rows, tasks_dirs)
    excluded = Counter()
    for row in rows:
        if isinstance(row, dict) and "_invalid_json" not in row and stats.is_valid_result_row(row):
            failure_class = stats.class_for_report(row)
            if failure_class in stats.EXCLUDED_FAILURE_CLASSES:
                excluded[failure_class] += 1
    return filtered["countable_rows"], dict(sorted(excluded.items()))


def _unique_cells(rows):
    cells = defaultdict(list)
    for row in rows:
        cells[(row["task"], row["trial"])].append(row)
    duplicates = sum(1 for values in cells.values() if len(values) != 1)
    return {cell: values[0] for cell, values in cells.items() if len(values) == 1}, duplicates


def _sum_per_solve(rows, field, solved):
    if not solved:
        return None
    values = [row.get(field) for row in rows]
    if not values or any(not stats.is_nonnegative_number(value) for value in values):
        return None
    return sum(values) / solved


def build_comparison(paths, tasks_dirs=None):
    """Return a report object for matched arms in ``paths``."""
    arms = load_arms(paths)
    if len(arms) < 2:
        raise ValueError("comparison requires at least two arms")

    task_roots = stats.parse_tasks_dirs(tasks_dirs)
    eligible = {}
    exclusions = {}
    duplicate_counts = {}
    for arm, rows in arms.items():
        countable, exclusions[arm] = _filter_arm(rows, task_roots)
        eligible[arm], duplicate_counts[arm] = _unique_cells(countable)

    common = set.intersection(*(set(cells) for cells in eligible.values()))
    matched = {arm: [eligible[arm][cell] for cell in sorted(common)] for arm in sorted(arms)}
    summaries = {}
    for arm, rows in matched.items():
        # Canonical aggregation owns both Wilson and hack-adjusted score math.
        if rows:
            canonical = stats.aggregate_table(rows, (), min_n=0)[0]
        else:
            canonical = {
                "solved": 0,
                "n": 0,
                "solve_rate": None,
                "wilson95": list(stats.wilson_ci(0, 0)),
                "mean_score": None,
            }
        solved = canonical["solved"]
        versions = sorted({str(row["harness_version"]) for row in rows
                           if row.get("harness_version") not in (None, "")})
        summaries[arm] = {
            "solved": solved,
            "n": canonical["n"],
            "solve_rate": canonical["solve_rate"],
            "wilson95": canonical["wilson95"],
            "hack_adjusted_rate": canonical["mean_score"],
            "wall_time_per_solve": _sum_per_solve(rows, "wall_time_s", solved),
            "tokens_input_uncached_per_solve": _sum_per_solve(
                rows, "tokens_input_uncached", solved),
            "tokens_cache_read_per_solve": _sum_per_solve(rows, "tokens_cache_read", solved),
            "tokens_output_per_solve": _sum_per_solve(rows, "tokens_output", solved),
            "versions": versions,
            "version_mixed": len(versions) > 1,
            "excluded": exclusions[arm],
            "duplicate_cells_excluded": duplicate_counts[arm],
            "unmatched_countable_rows": len(eligible[arm]) - len(common),
        }

    return {
        "inputs": list(paths),
        "arms": sorted(arms),
        "matched_n": len(common),
        "summaries": summaries,
    }


def _pct(value):
    return "-" if value is None else f"{value * 100:.1f}%"


def _number(value, digits=1):
    return "-" if value is None else f"{value:.{digits}f}"


def _versions(summary):
    if not summary["versions"]:
        return "unknown"
    value = ", ".join(summary["versions"])
    return value + (" [MIXED]" if summary["version_mixed"] else "")


def scorecard_rows(report):
    """Return scorecard rows as ``(metric, [arm values...])``."""
    summaries = report["summaries"]
    rows = [
        ("Harness version", [_versions(summaries[arm]) for arm in report["arms"]]),
        ("Solved", [f"{summaries[arm]['solved']}/{summaries[arm]['n']}" for arm in report["arms"]]),
        ("Solve rate", [_pct(summaries[arm]["solve_rate"]) for arm in report["arms"]]),
        ("Wilson 95% CI", [f"[{summaries[arm]['wilson95'][0]:.3f}, "
                           f"{summaries[arm]['wilson95'][1]:.3f}]" for arm in report["arms"]]),
        ("Hack-adjusted rate", [_pct(summaries[arm]["hack_adjusted_rate"])
                                for arm in report["arms"]]),
        ("Wall time / solve (s)", [_number(summaries[arm]["wall_time_per_solve"], 2)
                                   for arm in report["arms"]]),
    ]
    for label, field in TOKEN_METRICS:
        key = field + "_per_solve"
        rows.append((label, [_number(summaries[arm][key]) for arm in report["arms"]]))
    rows.extend([
        ("Excluded: infra", [str(summaries[arm]["excluded"].get("infra", 0))
                             for arm in report["arms"]]),
        ("Excluded: rate_limited", [str(summaries[arm]["excluded"].get("rate_limited", 0))
                                    for arm in report["arms"]]),
        ("Unmatched countable rows", [str(summaries[arm]["unmatched_countable_rows"])
                                      for arm in report["arms"]]),
        ("Duplicate cells excluded", [str(summaries[arm]["duplicate_cells_excluded"])
                                      for arm in report["arms"]]),
    ])
    return rows


def _plain_table(headers, rows):
    widths = [len(value) for value in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def line(values):
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(values))

    return "\n".join([line(headers), line(["-" * width for width in widths])] +
                     [line(row) for row in rows])


def render_text(report):
    rows = [[metric] + values for metric, values in scorecard_rows(report)]
    return ("OpenBench matched comparison\n"
            f"Matched n: {report['matched_n']} (task, trial cells present in all arms)\n\n" +
            _plain_table(["Metric"] + report["arms"], rows))


def render_markdown(report):
    lines = [
        "# OpenBench comparison scorecard",
        "",
        f"**Matched n: {report['matched_n']}** `(task, trial)` cells present in every arm.",
        "",
        "| Metric | " + " | ".join(report["arms"]) + " |",
        "| --- | " + " | ".join("---:" for _ in report["arms"]) + " |",
    ]
    for metric, values in scorecard_rows(report):
        lines.append("| " + metric + " | " + " | ".join(values) + " |")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Compare OpenBench result arms")
    parser.add_argument("results", nargs="+", help="result JSONL path(s)")
    parser.add_argument("--markdown", help="write a Markdown scorecard to this path")
    parser.add_argument("--tasks-dir", action="append",
                        help="task root for DROPPED.md checks; repeatable or comma-separated")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        report = build_comparison(args.results, tasks_dirs=args.tasks_dir)
    except ValueError as exc:
        print(f"compare: error: {exc}", file=sys.stderr)
        return 2
    print(render_text(report))
    if args.markdown:
        parent = os.path.dirname(os.path.abspath(args.markdown))
        os.makedirs(parent, exist_ok=True)
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
