#!/usr/bin/env python3
"""Validate benchmark task checkers.

For every task directory under tasks/ this script confirms that the checker
behaves correctly at both ends of the spectrum:

  1. Run checker.sh against a fresh copy of workspace/ (the unsolved starting
     state). The checker MUST fail (nonzero exit) -- otherwise the task would
     be marked solved before the agent does anything.

  2. Run checker.sh against a fresh copy of workspace/ with the golden
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
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
TASKS_DIR = os.path.join(REPO_ROOT, "tasks")


def copy_tree(src, dst):
    """Copy the contents of src into dst (dst may already exist)."""
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_root = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_root, exist_ok=True)
        for name in files:
            shutil.copy2(os.path.join(root, name), os.path.join(target_root, name))


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


def run_checker(task_dir, overlay_solution):
    """Set up a workspace copy, optionally overlay solution/, run checker.sh.

    Returns (exit_code, combined_output, parsed_score).
    """
    workspace = os.path.join(task_dir, "workspace")
    solution = os.path.join(task_dir, "solution")
    checker = os.path.join(task_dir, "checker.sh")

    tmp = tempfile.mkdtemp(prefix="taskcheck-")
    try:
        copy_tree(workspace, tmp)
        if overlay_solution:
            copy_tree(solution, tmp)

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


def discover_tasks():
    if not os.path.isdir(TASKS_DIR):
        return []
    tasks = []
    for name in sorted(os.listdir(TASKS_DIR)):
        task_dir = os.path.join(TASKS_DIR, name)
        if not os.path.isdir(task_dir):
            continue
        if not os.path.isfile(os.path.join(task_dir, "checker.sh")):
            continue
        tasks.append(task_dir)
    return tasks


def fmt_score(value):
    return "-" if value is None else "{:.3f}".format(value)


def main():
    tasks = discover_tasks()
    if not tasks:
        print("No tasks with a checker.sh found under tasks/", file=sys.stderr)
        return 1

    results = []
    all_ok = True
    for task_dir in tasks:
        name = os.path.basename(task_dir)
        problems = []

        # Required structure.
        for required in ("instruction.md", "workspace", "solution", "checker.sh"):
            if not os.path.exists(os.path.join(task_dir, required)):
                problems.append("missing {}".format(required))

        ws_code = ws_out = sol_code = sol_out = None
        ws_score = sol_score = None
        if not problems:
            ws_code, ws_out, ws_raw = run_checker(task_dir, overlay_solution=False)
            sol_code, sol_out, sol_raw = run_checker(task_dir, overlay_solution=True)
            ws_score = effective_score(ws_code, ws_raw)
            sol_score = effective_score(sol_code, sol_raw)

            if ws_code == 0:
                problems.append("workspace checker passed (expected failure)")
            if sol_code != 0:
                problems.append("solution checker failed (expected pass)")
            # A checker that exits 0 but reports partial credit is inconsistent.
            if sol_code == 0 and sol_raw is not None and abs(sol_raw - 1.0) > 1e-9:
                problems.append(
                    "solution exited 0 but SCORE={:.3f} (expected 1.0)".format(sol_raw))

        ok = not problems
        all_ok = all_ok and ok
        results.append({
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

    # Table.
    name_w = max([len(r["name"]) for r in results] + [len("TASK")])
    header = "{:<{w}}  {:>10}  {:>10}  {:>10}  {:>10}  {:>6}".format(
        "TASK", "workspace", "base_score", "solution", "sol_score", "RESULT", w=name_w)
    print(header)
    print("-" * len(header))
    for r in results:
        ws = "FAIL(ok)" if (r["ws_code"] not in (None, 0)) else (
            "n/a" if r["ws_code"] is None else "PASS(bad)")
        sol = "PASS(ok)" if r["sol_code"] == 0 else (
            "n/a" if r["sol_code"] is None else "FAIL(bad)")
        result = "PASS" if r["ok"] else "FAIL"
        print("{:<{w}}  {:>10}  {:>10}  {:>10}  {:>10}  {:>6}".format(
            r["name"], ws, fmt_score(r["ws_score"]),
            sol, fmt_score(r["sol_score"]), result, w=name_w))

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
    if all_ok:
        print("All {} task(s) validated: workspace FAILs, solution PASSes "
              "(solution score 1.0).".format(len(results)))
        return 0
    print("Validation FAILED for one or more tasks.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
