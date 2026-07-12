#!/usr/bin/env python3
"""Canonical statistics tool for OpenBench JSONL results.

Exclusion rules (single source of truth for headline stats):

* Rows whose ``failure_class`` is ``infra`` or ``rate_limited`` are excluded
  from solve-rate denominators entirely. They are counted in the exclusion
  report by failure class.
* Rows for tasks whose task directory contains ``DROPPED.md`` are quarantined:
  they are excluded from denominators and reported separately as dropped-task
  quarantines. Task directories are resolved from ``--tasks-dir`` roots; by
  default the tool checks ``tasks/`` and ``tasks-imported/terminal-bench/``.
* Every other structurally usable row counts in denominators, whether solved or
  failed. Nothing is silently dropped.

The CLI prints an all-countable table labelled non-comparable, plus (when two
or more groups are present) a matched-denominator table restricted to cells that
are present in every compared group. Wilson 95% confidence intervals use a pure
stdlib implementation.
"""

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_TASK_DIRS = (
    os.path.join(REPO, "tasks"),
    os.path.join(REPO, "tasks-imported", "terminal-bench"),
)
EXCLUDED_FAILURE_CLASSES = {"infra", "rate_limited"}
GROUP_CHOICES = ("harness,model", "model", "harness")
Z_95 = 1.96

try:  # Package import path (`import bench.stats`).
    from .failure_class import class_for_report
except ImportError:  # Script/test path (`python3 bench/stats.py` or bench on sys.path).
    from failure_class import class_for_report


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_nonnegative_number(value):
    return is_number(value) and value >= 0


def wilson_ci(successes, n, z=Z_95):
    """Return Wilson score 95% CI for ``successes / n``, clamped to [0, 1]."""
    if n <= 0:
        return (0.0, 1.0)
    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = phat + z2 / (2.0 * n)
    margin = z * math.sqrt(phat * (1.0 - phat) / n + z2 / (4.0 * n * n))
    return (max(0.0, (center - margin) / denom), min(1.0, (center + margin) / denom))


def median(values):
    vals = sorted(float(v) for v in values if is_number(v))
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def load_rows(paths):
    rows = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    rows.append({
                        "_invalid_json": str(exc),
                        "_source": path,
                        "_lineno": lineno,
                    })
                    continue
                if not isinstance(row, dict):
                    row = {"_invalid_json": "row is not an object"}
                row.setdefault("_source", path)
                row.setdefault("_lineno", lineno)
                rows.append(row)
    return rows


def parse_tasks_dirs(values):
    if not values:
        return list(DEFAULT_TASK_DIRS)
    roots = []
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if part:
                roots.append(os.path.abspath(part))
    return roots


def _safe_join_under(root, *parts):
    root_real = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(root_real, *parts))
    if os.path.commonpath([root_real, candidate]) != root_real:
        return None
    return candidate


def _candidate_task_dirs(task, roots):
    task = str(task or "").strip()
    if not task:
        return []
    candidates = []
    if os.path.isabs(task):
        task_real = os.path.realpath(task)
        for root in roots:
            root_real = os.path.realpath(root)
            if os.path.commonpath([root_real, task_real]) == root_real:
                candidates.append(task_real)
    else:
        task_parts = task.split("/")
        if any(part in ("", ".", "..") for part in task_parts):
            return []
        split_task = task.split("/", 1)
        for root in roots:
            candidate = _safe_join_under(root, task)
            if candidate:
                candidates.append(candidate)
            if len(split_task) == 2 and os.path.basename(os.path.normpath(root)) == split_task[0]:
                candidate = _safe_join_under(root, split_task[1])
                if candidate:
                    candidates.append(candidate)
    # Preserve order while removing duplicates.
    seen = set()
    out = []
    for cand in candidates:
        norm = os.path.normpath(cand)
        if norm not in seen:
            out.append(norm)
            seen.add(norm)
    return out


def task_is_dropped(task, roots, cache):
    if task in cache:
        return cache[task]
    for task_dir in _candidate_task_dirs(task, roots):
        dropped = os.path.join(task_dir, "DROPPED.md")
        if os.path.isfile(dropped):
            cache[task] = dropped
            return dropped
    cache[task] = None
    return None


def total_tokens(row):
    if is_nonnegative_number(row.get("tokens_total")):
        return row.get("tokens_total")
    if is_nonnegative_number(row.get("tokens")):
        return row.get("tokens")
    return None


def input_tokens(row):
    if is_nonnegative_number(row.get("tokens_input")):
        return row.get("tokens_input")
    if is_nonnegative_number(row.get("tokens_input_uncached")):
        return row.get("tokens_input_uncached")
    return None


def output_tokens(row):
    if is_nonnegative_number(row.get("tokens_output")):
        return row.get("tokens_output")
    return None


def load_pricing(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("pricing JSON must be an object keyed by model")
    pricing = {}
    for model, item in data.items():
        if not isinstance(item, dict):
            continue
        inp = item.get("input_per_mtok")
        out = item.get("output_per_mtok")
        if is_nonnegative_number(inp) and is_nonnegative_number(out):
            pricing[str(model)] = {
                "input_per_mtok": float(inp),
                "output_per_mtok": float(out),
            }
    return pricing


def row_cost(row, pricing):
    if not pricing:
        return None
    model = str(row.get("model") or "")
    price = pricing.get(model)
    if not price:
        return None
    inp = input_tokens(row)
    out = output_tokens(row)
    if not is_number(inp) or not is_number(out):
        return None
    return (float(inp) / 1_000_000.0 * price["input_per_mtok"]
            + float(out) / 1_000_000.0 * price["output_per_mtok"])


def group_fields(group_arg):
    return tuple(part.strip() for part in group_arg.split(",") if part.strip())


def is_valid_result_row(row):
    """Return True for rows with the minimum schema needed for canonical stats."""
    if not isinstance(row, dict) or "_invalid_json" in row:
        return False
    if not all(isinstance(row.get(field), str) and row.get(field)
               for field in ("harness", "model", "task")):
        return False
    if not isinstance(row.get("trial"), int) or isinstance(row.get("trial"), bool):
        return False
    if not isinstance(row.get("success"), bool):
        return False
    return True


def group_key(row, fields):
    return tuple(str(row.get(field) or "-") for field in fields)


def group_label(key, fields):
    if len(fields) == 1:
        return key[0]
    return ",".join(f"{field}={value}" for field, value in zip(fields, key))


def matched_cell_key(row, fields):
    parts = [str(row.get("task") or "-"), str(row.get("trial") if row.get("trial") is not None else "-")]
    for field in ("harness", "model"):
        if field not in fields:
            parts.append(str(row.get(field) or "-"))
    return tuple(parts)


def _empty_acc(label):
    return {
        "group": label,
        "solved": 0,
        "n": 0,
        "score_values": [],
        "solved_wall_time_s": [],
        "solved_tokens_total": [],
        "solved_tokens_input": [],
        "solved_tokens_output": [],
        "solved_cost": [],
    }


def _add_row(acc, row, pricing):
    success = bool(row.get("success"))
    acc["n"] += 1
    if success:
        acc["solved"] += 1
        wall = row.get("wall_time_s")
        if is_nonnegative_number(wall):
            acc["solved_wall_time_s"].append(wall)
        tok_total = total_tokens(row)
        if is_number(tok_total):
            acc["solved_tokens_total"].append(tok_total)
        tok_input = input_tokens(row)
        if is_number(tok_input):
            acc["solved_tokens_input"].append(tok_input)
        tok_output = output_tokens(row)
        if is_number(tok_output):
            acc["solved_tokens_output"].append(tok_output)
        cost = row_cost(row, pricing)
        if is_number(cost):
            acc["solved_cost"].append(cost)
    score = row.get("score")
    if not is_number(score) or not (0.0 <= float(score) <= 1.0):
        score = 1.0 if success else 0.0
    acc["score_values"].append(float(score))


def summarize_acc(acc, min_n, include_cost):
    n = acc["n"]
    solved = acc["solved"]
    lo, hi = wilson_ci(solved, n)
    rate = solved / n if n else None
    score = (sum(acc["score_values"]) / len(acc["score_values"])) if acc["score_values"] else None
    out = {
        "group": acc["group"],
        "solved": solved,
        "n": n,
        "solve_rate": rate,
        "wilson95": [lo, hi],
        "mean_score": score,
        "median_wall_time_s_solved": median(acc["solved_wall_time_s"]),
        "median_tokens_total_solved": median(acc["solved_tokens_total"]),
        "median_tokens_input_solved": median(acc["solved_tokens_input"]),
        "median_tokens_output_solved": median(acc["solved_tokens_output"]),
        "low_n": n < min_n,
        "flags": ["LOW-N"] if n < min_n else [],
    }
    if include_cost:
        out["median_cost_solved"] = median(acc["solved_cost"])
    return out


def aggregate_table(rows, fields, min_n, pricing=None):
    accs = {}
    for row in rows:
        key = group_key(row, fields)
        label = group_label(key, fields)
        acc = accs.setdefault(key, _empty_acc(label))
        _add_row(acc, row, pricing)
    include_cost = pricing is not None
    return [summarize_acc(accs[key], min_n, include_cost) for key in sorted(accs)]


def filter_rows(rows, tasks_dirs):
    countable = []
    excluded_counts = Counter()
    quarantined_tasks = Counter()
    invalid_rows = 0
    dropped_cache = {}

    for row in rows:
        if "_invalid_json" in row:
            invalid_rows += 1
            excluded_counts["invalid_json"] += 1
            continue
        if not is_valid_result_row(row):
            invalid_rows += 1
            excluded_counts["invalid_row"] += 1
            continue
        task = row.get("task")
        dropped_path = task_is_dropped(task, tasks_dirs, dropped_cache)
        if dropped_path:
            excluded_counts["quarantined_dropped_task"] += 1
            quarantined_tasks[str(task)] += 1
            continue
        fc = class_for_report(row)
        if fc in EXCLUDED_FAILURE_CLASSES:
            excluded_counts[fc] += 1
            continue
        countable.append(row)
    return {
        "countable_rows": countable,
        "excluded_counts": dict(sorted(excluded_counts.items())),
        "quarantined_tasks": dict(sorted(quarantined_tasks.items())),
        "invalid_rows": invalid_rows,
    }


def matched_rows(countable_rows, fields):
    by_group = defaultdict(lambda: defaultdict(list))
    for row in countable_rows:
        gkey = group_key(row, fields)
        ckey = matched_cell_key(row, fields)
        by_group[gkey][ckey].append(row)
    if len(by_group) < 2:
        return list(countable_rows), None

    duplicate_cells = 0
    duplicate_rows = 0
    unique_by_group = {}
    for gkey, cells in by_group.items():
        unique_by_group[gkey] = {}
        for ckey, rows in cells.items():
            if len(rows) == 1:
                unique_by_group[gkey][ckey] = rows[0]
            else:
                # Duplicate benchmark cells are ambiguous for a matched table.
                # Keep all rows in all-countable stats, but exclude the duplicated
                # cell from matched denominators and report it explicitly.
                duplicate_cells += 1
                duplicate_rows += len(rows)

    common = None
    for cells in unique_by_group.values():
        keys = set(cells)
        common = keys if common is None else common & keys
    common = common or set()
    out = []
    for gkey in sorted(unique_by_group):
        for ckey in sorted(common):
            out.append(unique_by_group[gkey][ckey])
    diagnostics = {
        "groups_compared": len(by_group),
        "matched_cells_per_group": len(common),
        "matched_rows": len(out),
        "unmatched_countable_rows": len(countable_rows) - len(out),
        "duplicate_cells_excluded": duplicate_cells,
        "duplicate_rows_excluded": duplicate_rows,
    }
    return out, diagnostics


def build_stats(paths, group="harness,model", min_n=5, tasks_dirs=None, pricing=None):
    fields = group_fields(group)
    if group not in GROUP_CHOICES:
        raise ValueError(f"unsupported group {group!r}")
    tasks_dirs = parse_tasks_dirs(tasks_dirs)
    rows = load_rows(paths)
    filtered = filter_rows(rows, tasks_dirs)
    countable = filtered["countable_rows"]
    overall = aggregate_table(countable, fields, min_n, pricing=pricing)
    mrows, mdiag = matched_rows(countable, fields)
    matched = aggregate_table(mrows, fields, min_n, pricing=pricing) if mdiag else None
    return {
        "inputs": list(paths),
        "group": group,
        "group_fields": list(fields),
        "min_n": min_n,
        "tasks_dirs": tasks_dirs,
        "raw_rows": len(rows),
        "countable_rows": len(countable),
        "excluded_counts": filtered["excluded_counts"],
        "quarantined_tasks": filtered["quarantined_tasks"],
        "tables": {
            "all_countable_non_comparable": overall,
            "matched_comparable": matched,
        },
        "matched": mdiag,
        "pricing": {"enabled": pricing is not None},
    }


def fmt_pct(value):
    return "-" if value is None else f"{value * 100:.1f}%"


def fmt_num(value, digits=1):
    return "-" if value is None else f"{float(value):.{digits}f}"


def fmt_tokens(value):
    if value is None:
        return "-"
    value = float(value)
    return f"{value / 1000:.1f}k" if abs(value) >= 1000 else f"{value:.0f}"


def fmt_cost(value):
    return "-" if value is None else f"${float(value):.4f}"


def render_table(rows, include_cost=False):
    headers = [
        "group", "solved", "n", "rate", "wilson95", "score",
        "med_s/solve", "med_tok/solve", "med_in/solve", "med_out/solve",
        "flags",
    ]
    if include_cost:
        headers.insert(-1, "med_cost/solve")
    body = []
    for row in rows:
        cells = [
            row["group"],
            str(row["solved"]),
            str(row["n"]),
            fmt_pct(row["solve_rate"]),
            f"[{row['wilson95'][0]:.3f}, {row['wilson95'][1]:.3f}]",
            fmt_num(row["mean_score"], 3),
            fmt_num(row["median_wall_time_s_solved"], 2),
            fmt_tokens(row["median_tokens_total_solved"]),
            fmt_tokens(row["median_tokens_input_solved"]),
            fmt_tokens(row["median_tokens_output_solved"]),
            ",".join(row["flags"]) if row["flags"] else "-",
        ]
        if include_cost:
            cells.insert(-1, fmt_cost(row.get("median_cost_solved")))
        body.append(cells)
    widths = [len(h) for h in headers]
    for cells in body:
        for idx, cell in enumerate(cells):
            widths[idx] = max(widths[idx], len(cell))

    def line(cells):
        return "  ".join(str(cell).ljust(widths[idx]) for idx, cell in enumerate(cells))

    return "\n".join([line(headers), line(["-" * w for w in widths])] + [line(cells) for cells in body])


def render_text(stats):
    lines = []
    lines.append("OpenBench canonical stats")
    lines.append(f"Inputs: {', '.join(stats['inputs'])}")
    lines.append(f"Group: {stats['group']}   min_n: {stats['min_n']}")
    lines.append(f"Rows: raw={stats['raw_rows']} countable={stats['countable_rows']}")
    excluded = stats["excluded_counts"] or {}
    if excluded:
        lines.append("Excluded: " + ", ".join(f"{k}={v}" for k, v in sorted(excluded.items())))
    else:
        lines.append("Excluded: none")
    if stats["quarantined_tasks"]:
        lines.append("Quarantined dropped tasks: " + ", ".join(
            f"{task}={count}" for task, count in sorted(stats["quarantined_tasks"].items())))
    lines.append("")
    lines.append("ALL COUNTABLE ROWS (NON-COMPARABLE; denominators may differ)")
    include_cost = stats.get("pricing", {}).get("enabled", False)
    lines.append(render_table(stats["tables"]["all_countable_non_comparable"], include_cost=include_cost))
    matched = stats.get("matched")
    if matched:
        lines.append("")
        lines.append("MATCHED DENOMINATORS (COMPARABLE; cells present in every group)")
        diag = (
            f"Matched cells/group={matched['matched_cells_per_group']} "
            f"groups={matched['groups_compared']} unmatched_countable_rows={matched['unmatched_countable_rows']}"
        )
        if matched.get("duplicate_cells_excluded"):
            diag += (
                f" duplicate_cells_excluded={matched['duplicate_cells_excluded']}"
                f" duplicate_rows_excluded={matched['duplicate_rows_excluded']}"
            )
        lines.append(diag)
        lines.append(render_table(stats["tables"]["matched_comparable"], include_cost=include_cost))
    return "\n".join(lines)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Canonical OpenBench stats from results JSONL")
    parser.add_argument("results", nargs="+", help="results JSONL file(s)")
    parser.add_argument("--group", choices=GROUP_CHOICES, default="harness,model",
                        help="comparison grouping (default: harness,model)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--min-n", type=int, default=5, help="mark groups with n below this as LOW-N")
    parser.add_argument("--tasks-dir", action="append",
                        help="task root to inspect for DROPPED.md; may be repeated or comma-separated")
    parser.add_argument("--pricing", help="optional pricing JSON: {model: {input_per_mtok, output_per_mtok}}")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    pricing = load_pricing(args.pricing) if args.pricing else None
    stats = build_stats(
        args.results,
        group=args.group,
        min_n=args.min_n,
        tasks_dirs=args.tasks_dir,
        pricing=pricing,
    )
    if args.json:
        print(json.dumps(stats, indent=2, sort_keys=True, allow_nan=False))
    else:
        print(render_text(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
