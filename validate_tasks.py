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


def run_checker(task_dir, overlay_solution):
    """Set up a workspace copy, optionally overlay solution/, run checker.sh.

    Returns (exit_code, combined_output).
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
        return proc.returncode, proc.stdout
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
        ws_ok = sol_ok = False
        if not problems:
            ws_code, ws_out = run_checker(task_dir, overlay_solution=False)
            sol_code, sol_out = run_checker(task_dir, overlay_solution=True)
            ws_ok = ws_code != 0        # workspace must FAIL
            sol_ok = sol_code == 0      # solution must PASS
            if not ws_ok:
                problems.append("workspace checker passed (expected failure)")
            if not sol_ok:
                problems.append("solution checker failed (expected pass)")

        ok = not problems
        all_ok = all_ok and ok
        results.append({
            "name": name,
            "ws_code": ws_code,
            "sol_code": sol_code,
            "ok": ok,
            "problems": problems,
            "ws_out": ws_out,
            "sol_out": sol_out,
        })

    # Table.
    name_w = max(len(r["name"]) for r in results)
    name_w = max(name_w, len("TASK"))
    header = "{:<{w}}  {:>13}  {:>13}  {:>6}".format(
        "TASK", "workspace", "solution", "RESULT", w=name_w)
    print(header)
    print("-" * len(header))
    for r in results:
        ws = "FAIL(ok)" if (r["ws_code"] not in (None, 0)) else (
            "n/a" if r["ws_code"] is None else "PASS(bad)")
        sol = "PASS(ok)" if r["sol_code"] == 0 else (
            "n/a" if r["sol_code"] is None else "FAIL(bad)")
        result = "PASS" if r["ok"] else "FAIL"
        print("{:<{w}}  {:>13}  {:>13}  {:>6}".format(
            r["name"], ws, sol, result, w=name_w))

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
        print("All {} task(s) validated: workspace FAILs, solution PASSes.".format(
            len(results)))
        return 0
    print("Validation FAILED for one or more tasks.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
