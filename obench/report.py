#!/usr/bin/env python3
"""Statistical report for the agent-harness comparison benchmark.

Reads ``results/results.jsonl`` and prints, per harness:

- a **success table**: per-task success (x/n), overall success rate with a
  Wilson 95% confidence interval, mean wall-clock time, tokens-per-solve, and
  mean turns; and
- an **efficiency summary** (the harness-tax view): success rate + Wilson CI,
  mean wall-time with its 95% CI half-width, tokens-per-solve, and turns-per-solve.

The efficiency metrics are normalised *per solved task* so they measure the cost
of getting a result, not just raw totals that scale with the trial count. Token
and turn data is optional per adapter: a harness that reports none shows ``-``
(never silently counted as zero), and per-solve figures need at least one solve.

Python3 stdlib only.
"""

import argparse
import json
import math
import os

from failure_class import FAILURE_CLASSES, class_for_report, is_excluded_from_solve_rate

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
          "per_task":    {task: [successes, n]},
          "succ": int, "n": int,        # solves and countable cells
          "taxonomy":    {failure_class: count},  # all cells, including excluded
          "scores":      [float, ...],  # per-countable-cell score (derived from success
                                        #   when the row predates the field)
          "wall_times":  [float, ...],  # per-cell wall-clock seconds
          "token_vals":  [int, ...],    # per-cell tokens, non-null only
          "turn_vals":   [int, ...],    # per-cell turns, non-null only
        }

    ``token_vals``/``turn_vals`` collect only rows that actually reported the
    value, so an empty list means "no data" (rendered ``-``) rather than zero.
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
            stats[harness] = {"per_task": {}, "succ": 0, "n": 0, "scores": [],
                              "wall_times": [], "token_vals": [], "turn_vals": [],
                              "taxonomy": {fc: 0 for fc in FAILURE_CLASSES},
                              "taxonomy_by_model": {}}
            harnesses.append(harness)
        if task not in tasks:
            tasks.append(task)

        st = stats[harness]
        fc = class_for_report(row)
        st["taxonomy"][fc] = st["taxonomy"].get(fc, 0) + 1
        model = row.get("model") or "-"
        key = (harness, model)
        model_counts = st["taxonomy_by_model"].setdefault(
            key, {klass: 0 for klass in FAILURE_CLASSES})
        model_counts[fc] = model_counts.get(fc, 0) + 1
        if is_excluded_from_solve_rate(row):
            continue

        pt = st["per_task"].setdefault(task, [0, 0])
        success = bool(row.get("success"))
        pt[1] += 1
        st["n"] += 1
        if success:
            pt[0] += 1
            st["succ"] += 1

        # Score is the partial-credit signal, averaged over all countable trials incl.
        # failures. Rows predating the field derive it from success (1.0/0.0).
        sc = row.get("score")
        if not isinstance(sc, (int, float)) or isinstance(sc, bool):
            sc = 1.0 if success else 0.0
        st["scores"].append(float(sc))

        wt = row.get("wall_time_s")
        if isinstance(wt, (int, float)) and not isinstance(wt, bool):
            st["wall_times"].append(wt)
        tok = row.get("tokens")
        if isinstance(tok, (int, float)) and not isinstance(tok, bool):
            st["token_vals"].append(tok)
        turn = row.get("turns")
        if isinstance(turn, (int, float)) and not isinstance(turn, bool):
            st["turn_vals"].append(turn)
    return harnesses, tasks, stats


# --- derived per-harness metrics (each returns None when undefined) ---------- #
def mean(vals):
    """Arithmetic mean, or None for an empty list."""
    return (sum(vals) / len(vals)) if vals else None


def ci_halfwidth(vals, z=1.96):
    """95% CI half-width of the mean (z * sd / sqrt(n)); None if < 2 samples."""
    n = len(vals)
    if n < 2:
        return None
    m = sum(vals) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in vals) / (n - 1))
    return z * sd / math.sqrt(n)


def tokens_per_solve(st):
    """Total reported tokens divided by number of solves; None if undefined.

    ``None`` when the harness reported no token data at all, or has no solves to
    normalise by. Reported tokens are never treated as zero when absent.
    """
    if not st["token_vals"] or st["succ"] == 0:
        return None
    return sum(st["token_vals"]) / st["succ"]


def turns_per_solve(st):
    """Total reported turns divided by number of solves; None if undefined."""
    if not st["turn_vals"] or st["succ"] == 0:
        return None
    return sum(st["turn_vals"]) / st["succ"]


# --- cell formatters --------------------------------------------------------- #
def _fmt_tokens(v):
    """Compact token count: '-' for None, 'N' below 1k, else 'X.Yk'."""
    if v is None:
        return "-"
    return f"{v / 1000:.1f}k" if v >= 1000 else f"{v:.0f}"


def _fmt_turns(v):
    """One-decimal turn figure, '-' for None."""
    return "-" if v is None else f"{v:.1f}"


def _fmt_secs(v):
    return "-" if v is None else f"{v:.2f}"


def _render(headers, rows_text):
    """Render a fixed-width text table (2-space gutter) from string cells."""
    widths = [len(h) for h in headers]
    for cells in rows_text:
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    lines = [fmt(headers), fmt(["-" * w for w in widths])]
    lines.extend(fmt(cells) for cells in rows_text)
    return "\n".join(lines)


def format_table(harnesses, tasks, stats):
    """Success table: per-task x/n, overall + Wilson CI, mean_s, tok/solve, turns.

    Design note: the raw total-token column was replaced by ``tok/slv``
    (tokens-per-solve) because a running total scales with the number of trials
    rather than describing the harness; ``turns`` (mean turns per cell) is added
    alongside. Both stay ``-`` when the adapter reports no data.
    """
    headers = ["harness"] + tasks + ["overall", "wilson95", "mscore", "mean_s", "tok/slv", "turns"]
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
        ms = mean(st["scores"])
        cells.append("-" if ms is None else f"{ms:.2f}")
        cells.append(_fmt_secs(mean(st["wall_times"])))
        cells.append(_fmt_tokens(tokens_per_solve(st)))
        cells.append(_fmt_turns(mean(st["turn_vals"])))
        rows_text.append(cells)
    return _render(headers, rows_text)


def format_efficiency(harnesses, stats):
    """Efficiency summary: the harness-tax view, one row per harness.

    Columns: success (x/n), rate, Wilson 95% CI, mean_s with its 95% CI
    half-width, tokens-per-solve, turns-per-solve.
    """
    headers = ["harness", "success", "rate", "wilson95", "mscore", "mean_s", "tok/slv", "turns/slv"]
    rows_text = []
    for harness in harnesses:
        st = stats[harness]
        n, succ = st["n"], st["succ"]
        rate = (succ / n) if n else 0.0
        lo, hi = wilson_ci(succ, n)
        m = mean(st["wall_times"])
        hw = ci_halfwidth(st["wall_times"])
        mean_s = "-" if m is None else (f"{m:.2f} ±{hw:.2f}" if hw is not None else f"{m:.2f}")
        ms = mean(st["scores"])
        rows_text.append([
            harness,
            f"{succ}/{n}" if n else "-",
            f"{rate:.0%}" if n else "-",
            f"[{lo:.3f}, {hi:.3f}]",
            "-" if ms is None else f"{ms:.2f}",
            mean_s,
            _fmt_tokens(tokens_per_solve(st)),
            _fmt_turns(turns_per_solve(st)),
        ])
    return _render(headers, rows_text)


def format_taxonomy(harnesses, stats):
    """Failure taxonomy counts per harness x model, including excluded rows."""
    headers = ["harness", "model"] + list(FAILURE_CLASSES)
    rows_text = []
    for harness in harnesses:
        items = sorted(stats[harness]["taxonomy_by_model"].items(), key=lambda kv: kv[0][1])
        for (_harness, model), counts in items:
            rows_text.append([harness, model] + [str(counts.get(fc, 0)) for fc in FAILURE_CLASSES])
    return _render(headers, rows_text)


def build_report(results_path):
    """Load, aggregate, and format the success and taxonomy tables."""
    rows = load_rows(results_path)
    if not rows:
        return f"No results found at {results_path}"
    harnesses, tasks, stats = aggregate(rows)
    return format_table(harnesses, tasks, stats) + "\n\nFailure taxonomy (all rows):\n" + format_taxonomy(harnesses, stats)


def build_efficiency_report(results_path):
    """Load, aggregate, and format the efficiency summary from a results file."""
    rows = load_rows(results_path)
    if not rows:
        return f"No results found at {results_path}"
    harnesses, _tasks, stats = aggregate(rows)
    return format_efficiency(harnesses, stats)


def build_taxonomy_report(results_path):
    """Load, aggregate, and format the failure taxonomy table."""
    rows = load_rows(results_path)
    if not rows:
        return f"No results found at {results_path}"
    harnesses, _tasks, stats = aggregate(rows)
    return format_taxonomy(harnesses, stats)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Benchmark results report.")
    parser.add_argument("--results-path", default=DEFAULT_RESULTS_PATH,
                        help="override the results.jsonl path")
    parser.add_argument("--efficiency", action="store_true",
                        help="print only the per-harness efficiency summary")
    args = parser.parse_args(argv)
    if args.efficiency:
        print(build_efficiency_report(args.results_path))
    else:
        print(build_report(args.results_path))
        print("\nEfficiency summary (per solved task):")
        print(build_efficiency_report(args.results_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
