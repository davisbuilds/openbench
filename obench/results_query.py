#!/usr/bin/env python3
"""Answer recurring results questions from raw JSONL, with evidence pointers.

Born 2026-07-25 after a week of answering "how do the models compare / what's
the coverage / are those failures real or infra" with fresh throwaway scripts
each time -- which produced inconsistent definitions and, twice, wrong numbers
(a mixed-basis 12x token claim; solve rates quoted at 76% coverage). This
module gives those questions ONE set of definitions and prints, for every
number, where it came from so a human can verify it with grep.

Definitions (shared with obench.report):
  judged     a cell whose failure_class is NOT excluded (infra/rate_limited/
             stalled) -- i.e. the model got a real verdict.
  planned    every distinct (task, trial) cell that has any row at all.
  coverage   |judged cells| / |planned cells|. Solve rates at low coverage
             skew HIGH: excluded cells run longer and long cells are harder.
  matched    the subset of cells where EVERY selected arm has a verdict, so
             cross-arm comparisons hold task mix constant.

Usage:
  obench results summary  results/a.jsonl [more.jsonl ...] [--harness pi]
  obench results pertask  results/a.jsonl [--harness pi] [--exclude-task t]
  obench results matched  results/a.jsonl [--harness pi]
  obench results errors   results/a.jsonl [--arm "pi x inkling"]
  obench results evidence results/a.jsonl --run-id <id-substring>
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

from .failure_class import EXCLUDED_FROM_SOLVE_RATE, class_for_report
from .report import wilson_ci

EXCLUDED = tuple(EXCLUDED_FROM_SOLVE_RATE)


def load(paths):
    """Load rows from JSONL files; each row remembers its source file."""
    rows = []
    for path in paths:
        if not os.path.isfile(path):
            raise SystemExit(f"no such results file: {path}")
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                row["_src"] = f"{path}:{lineno}"
                rows.append(row)
    return rows


def arm_of(row):
    return f"{row.get('harness', '?').split('@')[0]} x {row.get('model', '?')}"


def cell_of(row):
    return (row.get("task"), row.get("trial"))


def is_judged(row):
    # class_for_report, not the raw field: it corrects a stored exclusion the
    # row's own fields refute (a 429 mid-run that did not stop the checker from
    # reaching a verdict). Reading the raw field here would make this tool
    # disagree with `obench report` on the same file, which is the exact
    # inconsistency this module exists to remove.
    return class_for_report(row) not in EXCLUDED


def _filter(rows, args):
    out = rows
    if getattr(args, "harness", None):
        out = [r for r in out if r.get("harness", "").split("@")[0] == args.harness]
    if getattr(args, "model", None):
        out = [r for r in out if r.get("model") == args.model]
    for t in getattr(args, "exclude_task", None) or []:
        out = [r for r in out if r.get("task") != t]
    if getattr(args, "arm", None):
        out = [r for r in out if arm_of(r) == args.arm]
    return out


def _rank(row):
    """Preference key for choosing one row per cell: a judged verdict always
    beats an excluded attempt; among rows of equal judged-ness the latest
    ts_iso wins, so a --force rerun deterministically supersedes the attempt it
    replaced instead of the winner depending on line order in the file."""
    return (is_judged(row), row.get("ts_iso") or "")


def _arm_cells(rows):
    """arm -> {cell: chosen row}: judged beats excluded, then latest ts wins."""
    cells = collections.defaultdict(dict)
    for r in rows:
        key, cell = arm_of(r), cell_of(r)
        prev = cells[key].get(cell)
        if prev is None or _rank(r) > _rank(prev):
            cells[key][cell] = r
    return cells


def cmd_summary(rows, args):
    cells = _arm_cells(rows)
    print(f"{'arm':<32}{'solved':>8}{'judged':>8}{'planned':>9}{'rate':>7}"
          f"{'coverage':>10}  wilson95")
    for armname in sorted(cells):
        byc = cells[armname]
        judged = [r for r in byc.values() if is_judged(r)]
        solved = [r for r in judged if r.get("success")]
        n, s, planned = len(judged), len(solved), len(byc)
        lo, hi = wilson_ci(s, n)
        cov = n / planned if planned else 0.0
        flag = "" if cov >= 0.95 else " !"
        print(f"{armname:<32}{s:>8}{n:>8}{planned:>9}"
              f"{(s / n if n else 0):>7.0%}{cov:>9.0%}{flag:<2} [{lo:.2f},{hi:.2f}]")
    srcs = sorted({r['_src'].split(':')[0] for r in rows})
    print(f"\nevidence: {sum(1 for _ in rows)} rows from {', '.join(srcs)}; "
          f"excluded classes = {EXCLUDED}; a cell counts once "
          f"(judged beats excluded, then latest ts_iso wins).")
    _warn_low_coverage(cells)
    _warn_missing_cells(cells)
    _warn_mixed_hosts(cells)


def _warn_missing_cells(cells):
    """Flag arms that are missing whole cells from the grid the other arms ran.

    ``planned`` counts cells that produced at least one ROW, so a cell that never
    ran at all is invisible: pi x deepseek-v4-flash reported coverage 100% on
    tb-mid while actually holding 14 of 18 cells, because its 4 missing cells had
    no rows to be counted. That is precisely the case coverage exists to catch.

    The reference grid is the union of tasks across all selected arms x the
    highest trial number seen. An arm that deliberately ran a subset still gets
    flagged, which is correct -- it cannot be compared on the full grid.
    """
    tasks = {t for byc in cells.values() for (t, _) in byc}
    trials = {tr for byc in cells.values() for (_, tr) in byc}
    if not tasks or not trials:
        return
    grid = {(t, tr) for t in tasks for tr in trials}
    for armname in sorted(cells):
        absent = grid - set(cells[armname])
        if not absent:
            continue
        sample = ", ".join(f"{t}#t{tr}" for t, tr in sorted(absent)[:4])
        print(f"  MISSING-CELLS {armname}: {len(cells[armname])} of {len(grid)} "
              f"grid cells ever ran; {len(absent)} never produced a row "
              f"(e.g. {sample}). True coverage is below the figure above.")


def _warn_mixed_hosts(cells):
    """Flag arms whose cells came from more than one machine, and comparisons
    whose arms ran on different machines.

    The estate is two hosts with different container runtimes (Docker Desktop vs
    colima) and different available API keys, so an arm lands wherever its key
    exists: tb-mid ran deepseek on the laptop and laguna/inkling on the mini.
    Solve rates survive that, WALL TIME does not -- and rows written before the
    `host` field existed cannot be attributed at all, which is its own warning.
    """
    hosts_by_arm = {}
    unattributed = []
    for armname, byc in cells.items():
        hosts = {r.get("host") for r in byc.values() if r.get("host")}
        if len(byc) != sum(1 for r in byc.values() if r.get("host")):
            unattributed.append(armname)
        if hosts:
            hosts_by_arm[armname] = hosts
        if len(hosts) > 1:
            print(f"  MIXED-HOST WARNING {armname}: cells from {sorted(hosts)}; "
                  f"latency is not comparable within this arm.")
    distinct = {h for hs in hosts_by_arm.values() for h in hs}
    if len(hosts_by_arm) > 1 and len(distinct) > 1:
        print(f"  MIXED-HOST WARNING: arms ran on different machines "
              f"({ {a: sorted(hs) for a, hs in sorted(hosts_by_arm.items())} }); "
              f"compare solve rates, NOT wall time.")
    if unattributed:
        print(f"  HOST-UNKNOWN: {len(unattributed)} arm(s) have cells with no "
              f"`host` field (written before it was recorded): "
              f"{sorted(unattributed)[:4]}; their machine cannot be verified.")


def _warn_low_coverage(cells):
    for armname in sorted(cells):
        byc = cells[armname]
        missing = [c for c, r in byc.items() if not is_judged(r)]
        if missing and len(missing) / len(byc) > 0.05:
            sample = ", ".join(f"{t}#t{tr}" for t, tr in sorted(missing)[:4])
            print(f"  COVERAGE WARNING {armname}: {len(missing)} cells have no "
                  f"verdict (e.g. {sample}); rate likely skews HIGH.")


def cmd_pertask(rows, args):
    cells = _arm_cells(rows)
    arms = sorted(cells)
    tasks = sorted({c[0] for byc in cells.values() for c in byc})
    header = f"{'task':<32}" + "".join(f"{a.split(' x ')[-1][:12]:>14}" for a in arms)
    print(header)
    for t in tasks:
        line = f"{t[:31]:<32}"
        for a in arms:
            judged = [r for (task, _), r in cells[a].items()
                      if task == t and is_judged(r)]
            s = sum(1 for r in judged if r.get("success"))
            line += f"{f'{s}/{len(judged)}' if judged else '--':>14}"
        print(line)
    print("\nevidence: cell = solved/judged for that task; '--' = no verdicts "
          "(look in `errors` for why).")


def cmd_matched(rows, args):
    cells = _arm_cells(rows)
    arms = sorted(cells)
    if len(arms) < 2:
        raise SystemExit("matched needs >=2 arms after filtering")
    common = None
    for a in arms:
        judged = {c for c, r in cells[a].items() if is_judged(r)}
        common = judged if common is None else common & judged
    print(f"matched cells (every arm has a verdict): {len(common)}")
    for a in arms:
        s = sum(1 for c in common if cells[a][c].get("success"))
        n = len(common)
        lo, hi = wilson_ci(s, n)
        print(f"  {a:<32}{s:>3}/{n:<4}= {s / n if n else 0:>4.0%}  [{lo:.2f},{hi:.2f}]")
    dropped = {a: len({c for c, r in cells[a].items() if is_judged(r)}) - len(common)
               for a in arms}
    print(f"\nevidence: matched run_ids share (task, trial); per-arm judged cells "
          f"dropped to match: { {a.split(' x ')[-1]: d for a, d in dropped.items()} }")
    sample = sorted(common)[:5]
    print(f"sample matched cells: {[f'{t}#t{tr}' for t, tr in sample]}")


def cmd_errors(rows, args):
    cells = _arm_cells(rows)
    for armname in sorted(cells):
        byc = cells[armname]
        tax = collections.Counter(class_for_report(r) for r in byc.values())
        real = sum(v for k, v in tax.items() if k in ("wrong_answer", "timeout"))
        excl = sum(v for k, v in tax.items() if k in EXCLUDED)
        print(f"{armname}: {dict(tax)}")
        print(f"  -> real model failures: {real} (wrong_answer/timeout, in "
              f"denominator); infra-family: {excl} (excluded, not the model's fault)")
        for fc in EXCLUDED:
            bad = [r for r in byc.values() if class_for_report(r) == fc][:2]
            for r in bad:
                err = str(r.get("error") or r.get("output_tail") or "")[:90]
                print(f"     {fc}: {r.get('run_id')}  [{r['_src']}]  {err!r}")


def cmd_evidence(rows, args):
    needle = args.run_id or ""
    hits = [r for r in rows if needle in str(r.get("run_id", ""))]
    if not hits:
        raise SystemExit(f"no rows match run_id substring {needle!r}")
    for r in hits:
        print(f"{r['_src']}  {r.get('run_id')}")
        for k in ("failure_class", "success", "score", "checker_exit", "wall_time_s",
                  "tokens_output", "tokens_proxy_output", "turns", "ts_iso", "error"):
            if r.get(k) is not None:
                print(f"   {k} = {str(r[k])[:120]}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="obench results", description=__doc__)
    ap.add_argument("command",
                    choices=["summary", "pertask", "matched", "errors", "evidence"])
    ap.add_argument("paths", nargs="+", help="results JSONL file(s)")
    ap.add_argument("--harness")
    ap.add_argument("--model")
    ap.add_argument("--arm")
    ap.add_argument("--exclude-task", action="append")
    ap.add_argument("--run-id")
    args = ap.parse_args(argv)
    rows = _filter(load(args.paths), args)
    if not rows:
        raise SystemExit("no rows after filtering")
    {"summary": cmd_summary, "pertask": cmd_pertask, "matched": cmd_matched,
     "errors": cmd_errors, "evidence": cmd_evidence}[args.command](rows, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
