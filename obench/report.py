#!/usr/bin/env python3
"""Statistical report for the agent-harness comparison benchmark.

Reads ``results/results.jsonl`` and prints, per ``(harness, model)`` arm:

- a **success table**: per-task success (x/n), overall success rate with a
  Wilson 95% confidence interval, mean wall-clock time, tokens-per-solve, and
  mean turns; and
- an **efficiency summary** (the harness-tax view): success rate + Wilson CI,
  mean wall-time with its 95% CI half-width, tokens-per-solve, and turns-per-solve.

The efficiency metrics are normalised *per solved task* so they measure the cost
of getting a result, not just raw totals that scale with the trial count. Token
and turn data is optional per adapter: a harness that reports none shows ``-``
(never silently counted as zero), and per-solve figures need at least one solve.

Token totals are **basis-aware**: self-reported ``tokens`` when present, else the
proxy-measured fresh total (uncached input + output) when
``token_basis_proxy == "proxy_measured"``. Cache-read is never folded into
tok/slv. Proxy-derived figures are marked ``*`` with a footnote; mixed-basis
tables print a warning.

Python3 stdlib only.
"""

import argparse
import json
import math
import os

from .failure_class import FAILURE_CLASSES, class_for_report, is_excluded_from_solve_rate
from .config import load_config
from .paths import default_results_path
from .stats import (
    TOKEN_BASIS_PROXY,
    TOKEN_BASIS_SELF,
    effective_tokens,
)

DEFAULT_RESULTS_PATH = default_results_path()

PROXY_FOOTNOTE = (
    "* tok/slv is proxy-measured fresh tokens "
    "(tokens_proxy_input_uncached + tokens_proxy_output); "
    "cache-read is counted separately and not mixed into this total. "
    "Matches native adapters' self-reported tokens scalar."
)
MIXED_BASIS_WARNING = (
    "Warning: tok/slv bases differ across arms in this table "
    "(self-reported vs proxy-measured); compare with care."
)


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


def _arm_key(row):
    """Return the ``(harness, model)`` aggregate key for a result row."""
    harness = row.get("harness")
    if harness is None:
        return None
    model = row.get("model") or "-"
    return (harness, model)


def aggregate(rows):
    """Aggregate rows into per-``(harness, model)`` stats.

    Returns ``(arms, tasks, stats)`` where ``arms`` is an ordered list of
    ``(harness, model)`` keys and ``stats[arm]`` holds::

        {
          "per_task":    {task: [successes, n]},
          "succ": int, "n": int,        # solves and countable cells
          "taxonomy":    {failure_class: count},  # all cells, including excluded
          "scores":      [float, ...],  # per-countable-cell score (derived from success
                                        #   when the row predates the field)
          "wall_times":  [float, ...],  # per-cell wall-clock seconds
          "token_vals":  [int, ...],    # per-cell effective tokens, non-null only
          "token_bases": {str, ...},    # bases that contributed to token_vals
          "turn_vals":   [int, ...],    # per-cell turns, non-null only
        }

    ``token_vals``/``turn_vals`` collect only rows that actually reported the
    value, so an empty list means "no data" (rendered ``-``) rather than zero.
    Effective tokens prefer self-reported ``tokens``, else proxy fresh totals.
    """
    arms = []
    tasks = []
    stats = {}
    for row in rows:
        key = _arm_key(row)
        task = row.get("task")
        if key is None or task is None:
            continue
        if key not in stats:
            stats[key] = {"per_task": {}, "succ": 0, "n": 0, "scores": [],
                          "wall_times": [], "token_vals": [], "token_bases": set(),
                          "turn_vals": [],
                          "taxonomy": {fc: 0 for fc in FAILURE_CLASSES}}
            arms.append(key)
        if task not in tasks:
            tasks.append(task)

        st = stats[key]
        fc = class_for_report(row)
        st["taxonomy"][fc] = st["taxonomy"].get(fc, 0) + 1
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
        tok, basis = effective_tokens(row)
        if tok is not None:
            st["token_vals"].append(tok)
            if basis:
                st["token_bases"].add(basis)
        turn = row.get("turns")
        if isinstance(turn, (int, float)) and not isinstance(turn, bool):
            st["turn_vals"].append(turn)
    return arms, tasks, stats


# --- derived per-arm metrics (each returns None when undefined) -------------- #
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
    """Total effective tokens divided by number of solves; None if undefined.

    ``None`` when the arm reported no token data at all, or has no solves to
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


def token_basis_is_proxy(st):
    """True when this arm's tok/slv comes only from proxy-measured rows."""
    bases = st.get("token_bases") or set()
    return bool(bases) and bases == {TOKEN_BASIS_PROXY}


def table_has_mixed_token_bases(stats, arms):
    """True when some arms use self-reported tok/slv and others use proxy."""
    seen = set()
    for arm in arms:
        bases = stats[arm].get("token_bases") or set()
        if TOKEN_BASIS_SELF in bases:
            seen.add(TOKEN_BASIS_SELF)
        if TOKEN_BASIS_PROXY in bases:
            seen.add(TOKEN_BASIS_PROXY)
    return len(seen) > 1


# --- cell formatters --------------------------------------------------------- #
def _fmt_tokens(v, proxy_mark=False):
    """Compact token count: '-' for None, 'N' below 1k, else 'X.Yk'; optional *."""
    if v is None:
        return "-"
    text = f"{v / 1000:.1f}k" if v >= 1000 else f"{v:.0f}"
    return text + "*" if proxy_mark else text


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


def _token_notes(stats, arms, used_proxy):
    """Footnotes / warnings for tok/slv basis honesty."""
    notes = []
    if used_proxy:
        notes.append(PROXY_FOOTNOTE)
    if table_has_mixed_token_bases(stats, arms):
        notes.append(MIXED_BASIS_WARNING)
    return notes


def format_table(arms, tasks, stats):
    """Success table: per-task x/n, overall + Wilson CI, mean_s, tok/solve, turns.

    Design note: the raw total-token column was replaced by ``tok/slv``
    (tokens-per-solve) because a running total scales with the number of trials
    rather than describing the harness; ``turns`` (mean turns per cell) is added
    alongside. Both stay ``-`` when the adapter reports no data.
    """
    headers = ["harness", "model"] + tasks + [
        "overall", "wilson95", "mscore", "mean_s", "tok/slv", "turns",
    ]
    rows_text = []
    used_proxy = False
    for arm in arms:
        harness, model = arm
        st = stats[arm]
        cells = [harness, model]
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
        proxy_mark = token_basis_is_proxy(st)
        used_proxy = used_proxy or proxy_mark
        cells.append(_fmt_tokens(tokens_per_solve(st), proxy_mark=proxy_mark))
        cells.append(_fmt_turns(mean(st["turn_vals"])))
        rows_text.append(cells)
    text = _render(headers, rows_text)
    notes = _token_notes(stats, arms, used_proxy)
    if notes:
        text += "\n" + "\n".join(notes)
    return text


def format_efficiency(arms, stats):
    """Efficiency summary: the harness-tax view, one row per (harness, model).

    Columns: success (x/n), rate, Wilson 95% CI, mean_s with its 95% CI
    half-width, tokens-per-solve, turns-per-solve.
    """
    headers = [
        "harness", "model", "success", "rate", "wilson95", "mscore",
        "mean_s", "tok/slv", "turns/slv",
    ]
    rows_text = []
    used_proxy = False
    for arm in arms:
        harness, model = arm
        st = stats[arm]
        n, succ = st["n"], st["succ"]
        rate = (succ / n) if n else 0.0
        lo, hi = wilson_ci(succ, n)
        m = mean(st["wall_times"])
        hw = ci_halfwidth(st["wall_times"])
        mean_s = "-" if m is None else (f"{m:.2f} ±{hw:.2f}" if hw is not None else f"{m:.2f}")
        ms = mean(st["scores"])
        proxy_mark = token_basis_is_proxy(st)
        used_proxy = used_proxy or proxy_mark
        rows_text.append([
            harness,
            model,
            f"{succ}/{n}" if n else "-",
            f"{rate:.0%}" if n else "-",
            f"[{lo:.3f}, {hi:.3f}]",
            "-" if ms is None else f"{ms:.2f}",
            mean_s,
            _fmt_tokens(tokens_per_solve(st), proxy_mark=proxy_mark),
            _fmt_turns(turns_per_solve(st)),
        ])
    text = _render(headers, rows_text)
    notes = _token_notes(stats, arms, used_proxy)
    if notes:
        text += "\n" + "\n".join(notes)
    return text


def format_taxonomy(arms, stats):
    """Failure taxonomy counts per harness x model, including excluded rows."""
    headers = ["harness", "model"] + list(FAILURE_CLASSES)
    rows_text = []
    for arm in arms:
        harness, model = arm
        counts = stats[arm]["taxonomy"]
        rows_text.append(
            [harness, model] + [str(counts.get(fc, 0)) for fc in FAILURE_CLASSES]
        )
    return _render(headers, rows_text)


def build_report(results_path):
    """Load, aggregate, and format the success and taxonomy tables."""
    rows = load_rows(results_path)
    if not rows:
        return f"No results found at {results_path}"
    arms, tasks, stats = aggregate(rows)
    return (
        format_table(arms, tasks, stats)
        + "\n\nFailure taxonomy (all rows):\n"
        + format_taxonomy(arms, stats)
    )


def build_efficiency_report(results_path):
    """Load, aggregate, and format the efficiency summary from a results file."""
    rows = load_rows(results_path)
    if not rows:
        return f"No results found at {results_path}"
    arms, _tasks, stats = aggregate(rows)
    return format_efficiency(arms, stats)


def build_taxonomy_report(results_path):
    """Load, aggregate, and format the failure taxonomy table."""
    rows = load_rows(results_path)
    if not rows:
        return f"No results found at {results_path}"
    arms, _tasks, stats = aggregate(rows)
    return format_taxonomy(arms, stats)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Benchmark results report.")
    parser.add_argument("--results-path", default=None,
                        help="override the results.jsonl path "
                             "(default: from openbench.toml or <repo|cwd>/results/results.jsonl)")
    parser.add_argument("--efficiency", action="store_true",
                        help="print only the per-(harness, model) efficiency summary")
    args = parser.parse_args(argv)
    if args.results_path is None:
        cfg = load_config()
        args.results_path = cfg.results_path or default_results_path()
    if args.efficiency:
        print(build_efficiency_report(args.results_path))
    else:
        print(build_report(args.results_path))
        print("\nEfficiency summary (per solved task):")
        print(build_efficiency_report(args.results_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
