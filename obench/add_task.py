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
from .workspace import write_git_workspace_toml

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
  2. Put the unsolved starting state in workspace/ (or pin a git ref in workspace.toml).
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


def scaffold(task_path, from_dir=None, git_ref=None, git_subdir=None, git_repo="."):
    """Create a new task directory.

    Snapshot mode (default): create empty ``workspace/``, optionally seeded
    from ``from_dir``. Git mode: write ``workspace.toml`` instead of
    ``workspace/`` when ``git_ref`` is set.
    """
    if from_dir is not None and git_ref is not None:
        raise ValueError("use either --from or --git-ref, not both")
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
        solution = os.path.join(task_path, "solution")
        checker_data = os.path.join(task_path, "checker_data")
        os.makedirs(solution)
        os.makedirs(checker_data)

        if git_ref is not None:
            write_git_workspace_toml(
                task_path, git_ref, repo=git_repo or ".", subdir=git_subdir,
            )
        else:
            workspace = os.path.join(task_path, "workspace")
            os.makedirs(workspace)
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
    parser.add_argument(
        "--git-ref", metavar="REF",
        help="write workspace.toml pinned to REF instead of creating workspace/",
    )
    parser.add_argument(
        "--git-subdir", metavar="PATH",
        help="with --git-ref: only this repo subtree becomes the workspace root",
    )
    parser.add_argument(
        "--git-repo", default=".", metavar="REPO",
        help="with --git-ref: local path or URL (default: \".\" = repo containing the task)",
    )
    parser.add_argument("--gate", action="store_true", help="run obench.admission_gate on the new task immediately")
    args = parser.parse_args(argv)

    if args.git_subdir and not args.git_ref:
        parser.error("--git-subdir requires --git-ref")
    if args.git_repo != "." and not args.git_ref:
        parser.error("--git-repo requires --git-ref")

    try:
        task_path = scaffold(
            args.task_path,
            from_dir=args.from_dir,
            git_ref=args.git_ref,
            git_subdir=args.git_subdir,
            git_repo=args.git_repo,
        )
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
