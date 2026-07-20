#!/usr/bin/env python3
"""Scaffold a new OpenBench task directory."""

import argparse
import os
import shutil
import stat
import sys
from datetime import date

from . import admission_gate
from .paths import PACKAGE_DIR

HERE = PACKAGE_DIR


INSTRUCTION_TEMPLATE = """# TODO: Task title

TODO: Write the user-facing task instruction here. Be specific about the
required end state, constraints, and any files the agent should edit.

## Success criteria

- TODO: Describe observable behavior the checker will verify.
- TODO: Describe edge cases or non-goals.
"""

CHECKER_TEMPLATE = """#!/usr/bin/env bash
set -eu

TASK_DIR="${TASK_DIR:?TASK_DIR must point to this task directory}"

# TODO: Replace this scaffold with task-specific checks.
# Check files in the current working directory (a copied workspace/), and read
# immutable oracle data from "$TASK_DIR/checker_data" when needed.

if [ -f TODO_SOLUTION_FILE ]; then
  echo "PASS: TODO condition satisfied"
  echo "SCORE: 1.0"
  exit 0
fi

echo "FAIL: TODO condition was not satisfied"
echo "SCORE: 0.0"
exit 1
"""

PROVENANCE_TEMPLATE = """# Provenance

- Source: TODO
- Author: TODO
- Date: {today}
- License/permission notes: TODO
- Transformations from source: TODO
"""

NEXT_STEPS = """Next steps:
  1. Fill in every TODO in instruction.md, checker.sh, and PROVENANCE.md.
  2. Put the unsolved starting state in workspace/.
  3. Put the golden answer in solution/.
  4. Put checker-owned oracle data in checker_data/ if needed.
  5. Run: python3 -m obench.admission_gate {task_path}

This task is NOT admitted until the admission gate passes.
"""


def _path_is_within(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _reject_seed_symlinks(src):
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                rel = os.path.relpath(path, src)
                raise ValueError(f"--from source tree contains symlink: {rel}")


def _copy_contents(src, dst):
    _reject_seed_symlinks(src)
    for name in os.listdir(src):
        src_path = os.path.join(src, name)
        dst_path = os.path.join(dst, name)
        if os.path.isdir(src_path):
            shutil.copytree(src_path, dst_path)
        else:
            shutil.copy2(src_path, dst_path)


def scaffold(task_path, from_dir=None):
    task_path = os.path.abspath(task_path)
    if os.path.exists(task_path):
        raise FileExistsError(f"refusing to overwrite existing task dir: {task_path}")
    if from_dir is not None:
        from_dir = os.path.abspath(from_dir)
        if not os.path.isdir(from_dir):
            raise NotADirectoryError(f"--from must be an existing directory: {from_dir}")
        if _path_is_within(os.path.realpath(task_path), os.path.realpath(from_dir)):
            raise ValueError("task path must not be inside the --from source tree")

    os.makedirs(task_path)
    try:
        workspace = os.path.join(task_path, "workspace")
        solution = os.path.join(task_path, "solution")
        checker_data = os.path.join(task_path, "checker_data")
        os.makedirs(workspace)
        os.makedirs(solution)
        os.makedirs(checker_data)

        if from_dir is not None:
            _copy_contents(from_dir, workspace)

        with open(os.path.join(task_path, "instruction.md"), "w", encoding="utf-8") as fh:
            fh.write(INSTRUCTION_TEMPLATE)
        checker = os.path.join(task_path, "checker.sh")
        with open(checker, "w", encoding="utf-8") as fh:
            fh.write(CHECKER_TEMPLATE)
        os.chmod(checker, os.stat(checker).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        with open(os.path.join(task_path, "PROVENANCE.md"), "w", encoding="utf-8") as fh:
            fh.write(PROVENANCE_TEMPLATE.format(today=date.today().isoformat()))
    except Exception:
        shutil.rmtree(task_path, ignore_errors=True)
        raise
    return task_path


def run_gate(task_path):
    result = admission_gate.gate(task_path)
    admission_gate.print_human(result)
    return 0 if result["status"] != "FAIL" else admission_gate.EXIT_FINDINGS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_path", help="new <tasks-dir>/<task-name> directory")
    parser.add_argument("--from", dest="from_dir", help="copy an existing directory into workspace/ as the starting state")
    parser.add_argument("--gate", action="store_true", help="run obench.admission_gate on the new task immediately")
    args = parser.parse_args(argv)

    try:
        task_path = scaffold(args.task_path, from_dir=args.from_dir)
    except (FileExistsError, NotADirectoryError, ValueError, OSError) as exc:
        print(f"add_task: {exc}", file=sys.stderr)
        return 1

    print(f"Created task scaffold: {task_path}")
    print(NEXT_STEPS.format(task_path=task_path))
    if args.gate:
        return run_gate(task_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
