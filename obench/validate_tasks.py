#!/usr/bin/env python3
"""Validate benchmark task checkers.

For every task directory under the configured roots this script confirms that
the checker behaves correctly at both ends of the spectrum:

  1. Run checker.sh against a freshly materialized workspace (snapshot
     ``workspace/`` or git ``workspace.toml``). The checker MUST fail
     (nonzero exit) -- otherwise the task would be marked solved before the
     agent does anything.

  2. Run checker.sh against a freshly materialized workspace with the golden
     solution/ files overlaid on top. The checker MUST pass (exit 0) --
     otherwise a correct solution would be rejected.

The checker is invoked with cwd set to the temporary workspace copy and the
TASK_DIR environment variable pointing at the absolute task directory (so the
checker can reach its own checker_data/ without depending on cwd).

Partial-credit contract: a checker MAY print a line ``SCORE: <float 0.0-1.0>``
to stdout (last one wins). Exit 0 means fully solved and implies score 1.0; a
nonzero exit with a SCORE line means partial credit; no SCORE line falls back
to the binary interpretation (1.0 on exit 0, 0.0 otherwise). This script parses
that line so it can report the untouched-workspace baseline score and confirm
the golden solution reaches 1.0. Pass/fail polarity is still decided by the
exit code.

Prints a PASS/FAIL table and exits nonzero if any task's checker misbehaves.
Standard library only.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

from .config import load_config
from .paths import (
    TasksDirError,
    default_imported_tasks_dir,
    default_tasks_dir,
    resolve_tasks_dir,
)
from .workspace import (
    WorkspaceError,
    has_git_workspace,
    has_snapshot_workspace,
    materialize_workspace,
    overlay_solution,
)


def parse_score(output):
    """Return the last ``SCORE: <float>`` value in output, or None if absent."""
    score = None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("SCORE:"):
            try:
                score = float(stripped[len("SCORE:"):].strip())
            except ValueError:
                pass
    return score


def effective_score(exit_code, parsed_score):
    """Map (exit_code, parsed SCORE) to a 0..1 score per the contract."""
    if exit_code == 0:
        return 1.0
    if parsed_score is not None:
        return parsed_score
    return 0.0


def run_checker(task_dir, overlay_solution_flag):
    """Set up a workspace copy, optionally overlay solution/, run checker.sh.

    Returns (exit_code, combined_output, parsed_score).
    """
    checker = os.path.join(task_dir, "checker.sh")

    tmp = tempfile.mkdtemp(prefix="taskcheck-")
    try:
        try:
            materialize_workspace(task_dir, tmp)
        except WorkspaceError as exc:
            return 99, f"workspace materialization failed: {exc}\n", None
        if overlay_solution_flag:
            overlay_solution(task_dir, tmp)

        env = dict(os.environ)
        env["TASK_DIR"] = task_dir

        proc = subprocess.run(
            ["bash", checker],
            cwd=tmp,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return proc.returncode, proc.stdout, parse_score(proc.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def default_task_roots():
    """Return ``[(tier, root), ...]`` for the current working context."""
    roots = []
    tasks = default_tasks_dir()
    if tasks:
        roots.append(("core", tasks))
    imported = default_imported_tasks_dir()
    if imported:
        roots.append(("imported", imported))
    if roots:
        return roots
    # Fall back to resolve_tasks_dir error path via an empty list; callers
    # that need a hard failure should call resolve_tasks_dir first.
    return []


def discover_tasks(task_roots=None):
    """Find every task across the tiers.

    Returns a list of ``(tier, display_name, task_dir)``. A task is any
    directory containing a ``checker.sh``. Flat roots (``tasks/``) hold them
    one level deep; nested roots (``tasks-imported/``) nest them under a
    collection, so both are walked to any depth.
    """
    roots = list(task_roots) if task_roots is not None else default_task_roots()
    tasks = []
    for tier, root in roots:
        if not os.path.isdir(root):
            continue
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            if "checker.sh" in filenames:
                found.append(dirpath)
                dirnames[:] = []  # a task dir is a leaf; don't descend further
        for task_dir in sorted(found):
            display = os.path.relpath(task_dir, root)
            tasks.append((tier, display, task_dir))
    return tasks


def fmt_score(value):
    return "-" if value is None else "{:.3f}".format(value)


def build_task_roots(tasks_dir=None, include_imported=True):
    """Build tier roots from an optional ``--tasks-dir`` override."""
    if tasks_dir:
        path = os.path.abspath(tasks_dir)
        if not os.path.isdir(path):
            raise TasksDirError(
                f"tasks directory not found: {path}\n"
                "Pass --tasks-dir pointing at a directory of OpenBench tasks."
            )
        return [("tasks", path)]
    roots = default_task_roots()
    if roots:
        if not include_imported:
            return [(tier, root) for tier, root in roots if tier == "core"]
        return roots
    # No cwd discovery — raise a clear error.
    resolve_tasks_dir(None)
    return []  # pragma: no cover


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate OpenBench task checker polarity.",
    )
    parser.add_argument(
        "--tasks-dir", default=None,
        help="task root to validate (default: openbench.toml tasks_dir, else "
             "./tasks or ./.openbench/tasks; in a checkout also validates "
             "tasks-imported/)",
    )
    parser.add_argument(
        "--no-imported", action="store_true",
        help="skip tasks-imported/ even when running inside a checkout",
    )
    args = parser.parse_args(argv)

    if args.tasks_dir is None:
        cfg = load_config()
        if cfg.tasks_dir:
            args.tasks_dir = cfg.tasks_dir

    try:
        task_roots = build_task_roots(
            args.tasks_dir, include_imported=not args.no_imported,
        )
    except TasksDirError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    tasks = discover_tasks(task_roots)
    if not tasks:
        labels = ", ".join(root for _tier, root in task_roots) or "tasks/"
        print(f"No tasks with a checker.sh found under {labels}", file=sys.stderr)
        return 1

    results = []
    all_ok = True
    for tier, name, task_dir in tasks:
        problems = []

        # Required structure.
        for required in ("instruction.md", "solution", "checker.sh"):
            if not os.path.exists(os.path.join(task_dir, required)):
                problems.append("missing {}".format(required))
        snap = has_snapshot_workspace(task_dir)
        git = has_git_workspace(task_dir)
        if snap and git:
            problems.append("both workspace/ and workspace.toml present")
        elif not snap and not git:
            problems.append("missing workspace/ or workspace.toml")

        ws_code = ws_out = sol_code = sol_out = None
        ws_score = sol_score = None
        if not problems:
            ws_code, ws_out, ws_raw = run_checker(task_dir, overlay_solution_flag=False)
            sol_code, sol_out, sol_raw = run_checker(task_dir, overlay_solution_flag=True)
            ws_score = effective_score(ws_code, ws_raw)
            sol_score = effective_score(sol_code, sol_raw)

            if ws_code == 99 and ws_out and "workspace materialization failed" in ws_out:
                problems.append(ws_out.strip().splitlines()[0])
            elif ws_code == 0:
                problems.append("workspace checker passed (expected failure)")
            if sol_code == 99 and sol_out and "workspace materialization failed" in sol_out:
                problems.append(sol_out.strip().splitlines()[0])
            elif sol_code != 0:
                problems.append("solution checker failed (expected pass)")
            # A checker that exits 0 but reports partial credit is inconsistent.
            if sol_code == 0 and sol_raw is not None and abs(sol_raw - 1.0) > 1e-9:
                problems.append(
                    "solution exited 0 but SCORE={:.3f} (expected 1.0)".format(sol_raw))

        ok = not problems
        all_ok = all_ok and ok
        results.append({
            "tier": tier,
            "name": name,
            "ws_code": ws_code,
            "sol_code": sol_code,
            "ws_score": ws_score,
            "sol_score": sol_score,
            "ok": ok,
            "problems": problems,
            "ws_out": ws_out,
            "sol_out": sol_out,
        })

    # Table, grouped by tier so imported tasks are visibly separate from core.
    name_w = max([len(r["name"]) for r in results] + [len("TASK")])
    tier_w = max([len(r["tier"]) for r in results] + [len("TIER")])
    header = "{:<{tw}}  {:<{w}}  {:>10}  {:>10}  {:>10}  {:>10}  {:>6}".format(
        "TIER", "TASK", "workspace", "base_score", "solution", "sol_score",
        "RESULT", tw=tier_w, w=name_w)
    print(header)
    print("-" * len(header))
    seen_tiers = []
    for tier, _root in task_roots:
        if tier not in seen_tiers:
            seen_tiers.append(tier)
    for tier in seen_tiers:
        for r in results:
            if r["tier"] != tier:
                continue
            ws = "FAIL(ok)" if (r["ws_code"] not in (None, 0)) else (
                "n/a" if r["ws_code"] is None else "PASS(bad)")
            sol = "PASS(ok)" if r["sol_code"] == 0 else (
                "n/a" if r["sol_code"] is None else "FAIL(bad)")
            result = "PASS" if r["ok"] else "FAIL"
            print("{:<{tw}}  {:<{w}}  {:>10}  {:>10}  {:>10}  {:>10}  {:>6}".format(
                r["tier"], r["name"], ws, fmt_score(r["ws_score"]),
                sol, fmt_score(r["sol_score"]), result, tw=tier_w, w=name_w))

    # Detail for any failures.
    for r in results:
        if not r["ok"]:
            print("\n=== {} FAILED ===".format(r["name"]))
            for p in r["problems"]:
                print("  - {}".format(p))
            if r["ws_out"] is not None:
                print("  workspace checker exit={} output:".format(r["ws_code"]))
                for line in r["ws_out"].splitlines():
                    print("    | {}".format(line))
            if r["sol_out"] is not None:
                print("  solution checker exit={} output:".format(r["sol_code"]))
                for line in r["sol_out"].splitlines():
                    print("    | {}".format(line))

    print()
    per_tier = ", ".join(
        "{} {}".format(sum(1 for r in results if r["tier"] == tier), tier)
        for tier in seen_tiers
        if any(r["tier"] == tier for r in results))
    if all_ok:
        print("All {} task(s) validated ({}): workspace FAILs, solution PASSes "
              "(solution score 1.0).".format(len(results), per_tier))
        return 0
    print("Validation FAILED for one or more tasks.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
