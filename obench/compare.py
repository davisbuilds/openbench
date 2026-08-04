#!/usr/bin/env python3
"""Build a matched-denominator scorecard from OpenBench result JSONL files.

Each input file is an arm when multiple paths are supplied.  With one input,
rows are split by candidate name (when present) or harness.  Only unique
(task, trial) cells present in every arm contribute to the scorecard.
"""

import argparse
import html
import json
import os
import statistics
import sys
from collections import Counter, defaultdict

from . import stats
from .failure_class import has_near_zero_agent_tokens


TOKEN_METRICS = (
    ("Uncached input tokens", "tokens_input_uncached"),
    ("Cache-read tokens", "tokens_cache_read"),
    ("Cache-write tokens", "tokens_cache_write"),
    ("Output tokens", "tokens_output"),
)


def _path_labels(paths):
    """Return stable, globally unique labels derived from input filenames."""
    counts = Counter()
    emitted = set()
    labels = []
    for path in paths:
        base = os.path.basename(path)
        stem = os.path.splitext(base)[0] or base
        counts[stem] += 1
        label = stem if counts[stem] == 1 else f"{stem}-{counts[stem]}"
        while label in emitted:
            counts[stem] += 1
            label = f"{stem}-{counts[stem]}"
        emitted.add(label)
        labels.append(label)
    return labels


def _row_arm(row):
    provenance = row.get("candidate_provenance")
    if isinstance(provenance, dict) and provenance.get("name"):
        return str(provenance["name"])
    return str(row.get("harness") or "-")


def load_arms(paths):
    """Load paths and return ``({arm: rows}, unassigned_invalid_rows)``."""
    if len(paths) > 1:
        arms = defaultdict(list)
        for path, label in zip(paths, _path_labels(paths)):
            arms[label].extend(stats.load_rows([path]))
        return dict(arms), []

    rows = stats.load_rows(paths)
    valid_rows = [row for row in rows if stats.is_valid_result_row(row)]
    baseline_labels = {_row_arm(row) for row in valid_rows
                       if not isinstance(row.get("candidate_provenance"), dict)}
    identities = []
    for row in valid_rows:
        kind = "candidate" if isinstance(row.get("candidate_provenance"), dict) else "baseline"
        identity = (kind, _row_arm(row))
        if identity not in identities:
            identities.append(identity)

    labels = {}
    emitted = set()
    for kind, name in identities:
        base = name + " (candidate)" if kind == "candidate" and name in baseline_labels else name
        label = base
        suffix = 2
        while label in emitted:
            label = f"{base}-{suffix}"
            suffix += 1
        emitted.add(label)
        labels[(kind, name)] = label

    arms = defaultdict(list)
    unassigned = []
    for row in rows:
        kind = "candidate" if isinstance(row.get("candidate_provenance"), dict) else "baseline"
        identity = (kind, _row_arm(row))
        if identity in labels:
            arms[labels[identity]].append(row)
        else:
            unassigned.append(row)
    return dict(arms), unassigned


def _filter_arm(rows, tasks_dirs):
    filtered = stats.filter_rows(rows, tasks_dirs)
    return filtered["countable_rows"], filtered["excluded_counts"]


def _unique_cells(rows):
    cells = defaultdict(list)
    for row in rows:
        cells[(row["task"], row["trial"])].append(row)
    duplicates = sum(1 for values in cells.values() if len(values) != 1)
    return {cell: values[0] for cell, values in cells.items() if len(values) == 1}, duplicates


def _measurement(row, field):
    if (
        field.startswith("tokens_")
        and not stats.usage_evidence.ranking_eligible(row)
    ):
        return None
    value = row.get(field)
    if stats.is_nonnegative_number(value):
        if field == "wall_time_s":
            # Agree with report.py/stats.py: proxy pacing waits are not latency.
            paced = row.get("paced_wait_s")
            if stats.is_nonnegative_number(paced):
                value = max(0.0, value - paced)
        return value
    if field.startswith("tokens_"):
        # Honor the certification stats.py enforces: a truncated proxy capture
        # is not a full meter, so its per-request totals must not be summed as
        # if they were (68% of pool-vs-pi rows were truncated -- folding them
        # in silently diverges the compare/HTML totals from stats).
        if row.get("token_basis_proxy") != "proxy_measured":
            return None
        proxy_value = row.get("tokens_proxy_" + field.removeprefix("tokens_"))
        if stats.is_nonnegative_number(proxy_value):
            return proxy_value
    return None


def _sum_per_solve(rows, field, solved):
    if not solved:
        return None
    values = [_measurement(row, field) for row in rows]
    if not values or any(value is None for value in values):
        return None
    return sum(values) / solved


def _mean_median(rows, field):
    values = [_measurement(row, field) for row in rows]
    if not values or any(value is None for value in values):
        return None, None
    return sum(values) / len(values), statistics.median(values)


def _uniform_wall_cluster(rows):
    """Return the largest failure cluster whose wall times are within 5%."""
    values = sorted(float(row["wall_time_s"]) for row in rows
                    if not bool(row.get("success"))
                    and stats.is_nonnegative_number(row.get("wall_time_s")))
    best = []
    for start, low in enumerate(values):
        cluster = [value for value in values[start:] if value <= low * 1.05]
        if len(cluster) > len(best):
            best = cluster
    return best


def _arm_anomalies(arm, rows):
    """Detect signatures that commonly mean an arm failed before model work."""
    valid = [row for row in rows if stats.is_valid_result_row(row)]
    anomalies = []
    silent_wrong = [row for row in valid
                    if stats.class_for_report(row) == "wrong_answer"
                    and has_near_zero_agent_tokens(row)]
    if len(silent_wrong) >= 3:
        anomalies.append(
            f"ANOMALY [{arm}]: {len(silent_wrong)} wrong_answer cells have "
            "near-zero agent tokens")
    cluster = _uniform_wall_cluster(valid)
    if len(cluster) >= 3:
        anomalies.append(
            f"ANOMALY [{arm}]: {len(cluster)} failures have uniform wall times "
            f"within 5% ({min(cluster):.1f}-{max(cluster):.1f}s)")
    return anomalies


def build_comparison(paths, tasks_dirs=None, solved_intersection=False):
    """Return a report object for matched arms in ``paths``."""
    arms, unassigned_rows = load_arms(paths)
    if len(arms) < 2:
        raise ValueError("comparison requires at least two arms")

    task_roots = stats.parse_tasks_dirs(tasks_dirs)
    _, unassigned_excluded = _filter_arm(unassigned_rows, task_roots)
    eligible = {}
    exclusions = {}
    duplicate_counts = {}
    countable_counts = {}
    versions_by_arm = {}
    timeouts_by_arm = {}
    unknown_timeouts_by_arm = {}
    unknown_timeout_rows = 0
    provenance_rows = []
    anomalies = []
    for arm, rows in arms.items():
        anomalies.extend(_arm_anomalies(arm, rows))
        countable, exclusions[arm] = _filter_arm(rows, task_roots)
        eligible[arm], duplicate_counts[arm] = _unique_cells(countable)
        countable_counts[arm] = len(countable)
        versions_by_arm[arm] = sorted({str(row["harness_version"]) for row in countable
                                       if row.get("harness_version") not in (None, "")})
        timeout_values = [stats.provenance_value(row, "timeout_s") for row in countable]
        timeouts_by_arm[arm] = sorted({str(value) for value in timeout_values
                                      if value is not None})
        unknown_timeouts_by_arm[arm] = sum(value is None for value in timeout_values)
        unknown_timeout_rows += unknown_timeouts_by_arm[arm]
        timeout_count = sum(stats.class_for_report(row) == "timeout" for row in countable)
        if timeout_count:
            exclusions[arm]["timeout"] = timeout_count
        provenance_rows.extend(dict(row, _compare_arm=arm) for row in countable)

    provenance = stats.build_provenance(provenance_rows, ("_compare_arm",))
    common = set.intersection(*(set(cells) for cells in eligible.values()))
    common_cells = sorted(common)
    arm_names = sorted(arms)
    all_solved_cells = [
        cell for cell in common_cells
        if all(eligible[arm][cell]["success"] for arm in arm_names)
    ]
    matched = {arm: [eligible[arm][cell] for cell in common_cells] for arm in arm_names}
    all_solved = {
        arm: [eligible[arm][cell] for cell in all_solved_cells]
        for arm in arm_names
    }
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
        finished_rows = [row for row in rows if stats.class_for_report(row) != "timeout"]
        finished_n = len(finished_rows)
        finished_solved = sum(bool(row["success"]) for row in finished_rows)
        finished_rate = finished_solved / finished_n if finished_n else None
        versions = versions_by_arm[arm]
        timeouts = timeouts_by_arm[arm]
        efficiency_rows = all_solved[arm]
        wall_mean, wall_median = _mean_median(efficiency_rows, "wall_time_s")
        token_stats = {
            field: _mean_median(efficiency_rows, field)
            for _, field in TOKEN_METRICS
        }
        summaries[arm] = {
            "solved": solved,
            "n": canonical["n"],
            "solve_rate": canonical["solve_rate"],
            "wilson95": canonical["wilson95"],
            "finished_solved": finished_solved,
            "finished_n": finished_n,
            "finished_solve_rate": finished_rate,
            "finished_wilson95": list(stats.wilson_ci(finished_solved, finished_n)),
            "hack_adjusted_rate": canonical["mean_score"],
            "wall_time_per_solve": _sum_per_solve(rows, "wall_time_s", solved),
            "tokens_input_uncached_per_solve": _sum_per_solve(
                rows, "tokens_input_uncached", solved),
            "tokens_cache_read_per_solve": _sum_per_solve(rows, "tokens_cache_read", solved),
            "tokens_cache_write_per_solve": _sum_per_solve(rows, "tokens_cache_write", solved),
            "tokens_output_per_solve": _sum_per_solve(rows, "tokens_output", solved),
            "wall_time_per_cell_mean": wall_mean,
            "wall_time_per_cell_median": wall_median,
            **{
                field + "_per_cell_" + statistic: values[index]
                for field, values in token_stats.items()
                for index, statistic in enumerate(("mean", "median"))
            },
            "versions": versions,
            "version_mixed": len(versions) > 1,
            "timeouts": timeouts,
            "timeout_mixed": len(timeouts) > 1,
            "timeout_unknown": unknown_timeouts_by_arm[arm],
            "excluded": exclusions[arm],
            "duplicate_cells_excluded": duplicate_counts[arm],
            "unmatched_countable_rows": countable_counts[arm] - len(common),
        }

    return {
        "inputs": list(paths),
        "arms": arm_names,
        "matched_n": len(common_cells),
        "solved_intersection": solved_intersection,
        "all_solved_n": len(all_solved_cells),
        "summaries": summaries,
        "provenance_ok": provenance["ok"],
        "provenance": provenance,
        "unknown_timeout_rows": unknown_timeout_rows,
        "unassigned_excluded": unassigned_excluded,
        "anomalies": anomalies,
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


def _timeouts(summary):
    values = list(summary["timeouts"])
    if summary["timeout_unknown"]:
        values.append("unknown")
    value = ", ".join(values) if values else "unknown"
    return value + (" [MIXED]" if summary["timeout_mixed"] else "")


def _ci(summary, key):
    return f"[{summary[key][0]:.3f}, {summary[key][1]:.3f}]"


def scorecard_rows(report):
    """Return scorecard rows as ``(metric, [arm values...])``."""
    summaries = report["summaries"]
    rows = [
        ("Harness version", [_versions(summaries[arm]) for arm in report["arms"]]),
        ("Timeout cap (s)", [_timeouts(summaries[arm]) for arm in report["arms"]]),
        ("Solved", [f"{summaries[arm]['solved']}/{summaries[arm]['n']}" for arm in report["arms"]]),
        ("Solve rate", [_pct(summaries[arm]["solve_rate"]) for arm in report["arms"]]),
        ("Solve rate @cap", [_pct(summaries[arm]["solve_rate"]) for arm in report["arms"]]),
        ("Wilson 95% CI", [_ci(summaries[arm], "wilson95") for arm in report["arms"]]),
        ("Wilson 95% CI @cap", [_ci(summaries[arm], "wilson95")
                                for arm in report["arms"]]),
        ("Solved (finished)",
         [f"{summaries[arm]['finished_solved']}/{summaries[arm]['finished_n']}"
          for arm in report["arms"]]),
        ("Solve rate (finished)", [_pct(summaries[arm]["finished_solve_rate"])
                                   for arm in report["arms"]]),
        ("Wilson 95% CI (finished)", [_ci(summaries[arm], "finished_wilson95")
                                      for arm in report["arms"]]),
        ("Hack-adjusted rate", [_pct(summaries[arm]["hack_adjusted_rate"])
                                for arm in report["arms"]]),
    ]
    if report["solved_intersection"]:
        rows.extend([
            ("Wall time / cell mean (s)",
             [_number(summaries[arm]["wall_time_per_cell_mean"], 2)
              for arm in report["arms"]]),
            ("Wall time / cell median (s)",
             [_number(summaries[arm]["wall_time_per_cell_median"], 2)
              for arm in report["arms"]]),
        ])
        for label, field in TOKEN_METRICS:
            for statistic in ("mean", "median"):
                key = f"{field}_per_cell_{statistic}"
                rows.append((f"{label} / cell {statistic}",
                             [_number(summaries[arm][key]) for arm in report["arms"]]))
    else:
        rows.append(("Wall time / solve (s)",
                     [_number(summaries[arm]["wall_time_per_solve"], 2)
                      for arm in report["arms"]]))
        for label, field in TOKEN_METRICS:
            key = field + "_per_solve"
            rows.append((f"{label} / solve",
                         [_number(summaries[arm][key]) for arm in report["arms"]]))
    for category in ("infra", "rate_limited", "timeout", "invalid_json", "invalid_row",
                     "quarantined_dropped_task"):
        rows.append((f"Excluded: {category}", [str(summaries[arm]["excluded"].get(category, 0))
                                               for arm in report["arms"]]))
    rows.extend([
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


def _provenance_status(report):
    if report["provenance_ok"]:
        return "COMPARABILITY: OK"
    messages = "; ".join(flag["message"] for flag in report["provenance"]["flags"])
    return "NON-COMPARABLE PROVENANCE: " + messages


def _timeout_status(report):
    count = report["unknown_timeout_rows"]
    if not count:
        return None
    noun = "row" if count == 1 else "rows"
    return f"PROVENANCE WARNING: timeout_s unknown for {count} old {noun}"


def _unassigned_status(report):
    excluded = report["unassigned_excluded"]
    if not excluded:
        return "Unassigned exclusions: none"
    return "Unassigned exclusions: " + ", ".join(
        f"{key}={value}" for key, value in sorted(excluded.items()))


def _solved_intersection_status(report):
    if not report["solved_intersection"]:
        return None
    status = f"All-solved n: {report['all_solved_n']} of {report['matched_n']} matched"
    if report["all_solved_n"] == 0:
        status += " (no efficiency cells; efficiency metrics unavailable)"
    return status


def render_text(report):
    rows = [[metric] + values for metric, values in scorecard_rows(report)]
    solved_status = _solved_intersection_status(report)
    headline = f"Matched n: {report['matched_n']} (task, trial cells present in all arms)\n"
    if solved_status:
        headline += solved_status + "\n"
    statuses = [*report.get("anomalies", []), _provenance_status(report),
                _timeout_status(report), _unassigned_status(report)]
    return ("OpenBench matched comparison\n" + headline
            + "\n".join(status for status in statuses if status) + "\n\n"
            + _plain_table(["Metric"] + report["arms"], rows))


def _markdown_cell(value):
    escaped = html.escape(str(value), quote=False)
    return escaped.replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def render_markdown(report):
    arms = [_markdown_cell(arm) for arm in report["arms"]]
    lines = [
        "# OpenBench comparison scorecard",
        "",
        f"**Matched n: {report['matched_n']}** `(task, trial)` cells present in every arm.",
        "",
    ]
    solved_status = _solved_intersection_status(report)
    if solved_status:
        lines.extend([f"**{_markdown_cell(solved_status)}**", ""])
    statuses = [*report.get("anomalies", []), _provenance_status(report),
                _timeout_status(report), _unassigned_status(report)]
    lines.extend([
        *("> " + _markdown_cell(status) for status in statuses if status),
        "",
        "| Metric | " + " | ".join(arms) + " |",
        "| --- | " + " | ".join("---:" for _ in arms) + " |",
    ])
    for metric, values in scorecard_rows(report):
        cells = [_markdown_cell(metric)] + [_markdown_cell(value) for value in values]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Compare OpenBench result arms")
    parser.add_argument("results", nargs="+", help="result JSONL path(s)")
    parser.add_argument("--markdown", help="write a Markdown scorecard to this path")
    parser.add_argument("--tasks-dir", action="append",
                        help="task root for DROPPED.md checks; repeatable or comma-separated")
    parser.add_argument("--strict-provenance", action="store_true",
                        help="exit 2 when canonical comparison provenance differs")
    parser.add_argument("--solved-intersection", action="store_true",
                        help="compute efficiency only on cells every arm solved")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        report = build_comparison(
            args.results,
            tasks_dirs=args.tasks_dir,
            solved_intersection=args.solved_intersection,
        )
    except ValueError as exc:
        print(f"compare: error: {exc}", file=sys.stderr)
        return 2
    print(render_text(report))
    if args.markdown:
        parent = os.path.dirname(os.path.abspath(args.markdown))
        os.makedirs(parent, exist_ok=True)
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(report))
    if args.strict_provenance and not report["provenance_ok"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
