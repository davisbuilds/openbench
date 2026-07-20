#!/usr/bin/env python3
"""Scaffold ``.openbench/`` for private-repo harness evaluations.

    obench init
    obench init --task <name> [--from <dir>]

Idempotent: existing files are left untouched and noted.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from . import add_task
from .config import CONFIG_DIRNAME, CONFIG_FILENAME

OPENBENCH_TOML = """\
# OpenBench private-eval config.
# Paths are relative to the directory that contains .openbench/.
# Explicit CLI flags always override these defaults.

tasks_dir = ".openbench/tasks"
results_path = ".openbench/results/results.jsonl"

# Optional run defaults (uncomment to use):
# harnesses = ["null"]
# model = "gpt-5.5-medium"
# trials = 1
"""

GITIGNORE = """\
# Local-only benchmark artifacts (never commit unscrubbed transcripts).
results/
transcripts/
"""

TASKS_README = """\
# Private OpenBench tasks

Each task is a directory with:

```
<name>/
  instruction.md   # what the agent is told
  workspace/       # starting files (copied fresh per trial — keep it lean)
  solution/        # golden overlay for polarity checks only
  checker.sh       # exit 0 = solved; optional SCORE: <float>
  checker_data/    # optional oracle inputs kept out of workspace/
```

Scaffold a task from a slice of your repo:

```bash
obench init --task my-bug --from path/to/small/subdir
```

Then edit ``checker.sh`` / ``solution/`` until polarity passes:

```bash
obench validate
```

Private tasks are exempt from the public-contribution originality and
contamination rules in CONTRIBUTING-TASKS.md — those apply when contributing
to the shared public tiers.
"""

EXAMPLE_INSTRUCTION = """\
# Example: write greeting.txt

<!--
This is a commented example task skeleton under .openbench/tasks/example/.
Replace it with a real private task, or delete the example/ directory.

Guidance:
- Keep workspace/ small (it is copytree'd for every trial).
- Prefer an extracted slice of your codebase, not the whole monorepo.
- instruction.md must read like a normal engineering request (no mention of
  checkers, scores, or that this is a benchmark).
-->

Create a file named ``greeting.txt`` in the workspace root whose contents are
exactly:

```
hello
```
"""

EXAMPLE_CHECKER = """\
#!/usr/bin/env bash
set -eu

# Example checker: PASS when greeting.txt contains exactly "hello".
# Exit 0 = solved. Optional SCORE line for partial credit on harder tasks.

TASK_DIR="${TASK_DIR:?TASK_DIR must point to this task directory}"

if [ -f greeting.txt ] && [ "$(cat greeting.txt)" = "hello" ]; then
  echo "PASS: greeting.txt is correct"
  echo "SCORE: 1.0"
  exit 0
fi

echo "FAIL: greeting.txt missing or incorrect"
echo "SCORE: 0.0"
exit 1
"""

EXAMPLE_PROVENANCE = """\
# Provenance (private eval)

- Source: local example scaffold from `obench init`
- Author: local
- Date: private
- License/permission notes: n/a (not for public contribution)
- Transformations from source: n/a

Private-eval tasks are exempt from CONTRIBUTING-TASKS.md originality /
contamination rules for public tiers.
"""


def _openbench_root(start: str | None = None) -> str:
    return os.path.join(os.path.abspath(start or os.getcwd()), CONFIG_DIRNAME)


def _write_new(path: str, contents: str, notes: list[str]) -> bool:
    """Write ``contents`` only if ``path`` does not already exist.

    Returns True when a new file was written.
    """
    if os.path.exists(path):
        notes.append(f"skip (exists): {path}")
        return False
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(contents)
    notes.append(f"created: {path}")
    return True


def _ensure_dir(path: str, notes: list[str]) -> None:
    if os.path.isdir(path):
        notes.append(f"skip (exists): {path}/")
        return
    if os.path.exists(path):
        notes.append(f"skip (exists, not a directory): {path}")
        return
    os.makedirs(path)
    notes.append(f"created: {path}/")


def _valid_task_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name))


def scaffold_example_task(tasks_dir: str, notes: list[str]) -> None:
    """Create a polarity-valid commented example task if missing."""
    example = os.path.join(tasks_dir, "example")
    if os.path.exists(example):
        notes.append(f"skip (exists): {example}/")
        return
    os.makedirs(os.path.join(example, "workspace"), exist_ok=True)
    os.makedirs(os.path.join(example, "solution"), exist_ok=True)
    os.makedirs(os.path.join(example, "checker_data"), exist_ok=True)
    with open(os.path.join(example, "instruction.md"), "w", encoding="utf-8") as fh:
        fh.write(EXAMPLE_INSTRUCTION)
    checker = os.path.join(example, "checker.sh")
    with open(checker, "w", encoding="utf-8") as fh:
        fh.write(EXAMPLE_CHECKER)
    os.chmod(checker, 0o755)
    with open(os.path.join(example, "solution", "greeting.txt"), "w", encoding="utf-8") as fh:
        fh.write("hello")
    with open(os.path.join(example, "PROVENANCE.md"), "w", encoding="utf-8") as fh:
        fh.write(EXAMPLE_PROVENANCE)
    notes.append(f"created: {example}/ (commented example task)")


def init_scaffold(start: str | None = None) -> list[str]:
    """Create ``.openbench/`` scaffolding. Returns human-readable notes."""
    root = _openbench_root(start)
    notes: list[str] = []
    _ensure_dir(root, notes)
    _ensure_dir(os.path.join(root, "tasks"), notes)
    _ensure_dir(os.path.join(root, "results"), notes)
    _write_new(os.path.join(root, CONFIG_FILENAME), OPENBENCH_TOML, notes)
    _write_new(os.path.join(root, ".gitignore"), GITIGNORE, notes)
    _write_new(os.path.join(root, "tasks", "README.md"), TASKS_README, notes)
    scaffold_example_task(os.path.join(root, "tasks"), notes)
    return notes


def init_task(name: str, from_dir: str | None = None, start: str | None = None) -> str:
    """Create ``.openbench/tasks/<name>`` via add_task.scaffold."""
    if not _valid_task_name(name):
        raise ValueError(
            f"invalid task name {name!r}; use letters, digits, ., _, - "
            "(must start with alphanumeric)"
        )
    root = _openbench_root(start)
    tasks_dir = os.path.join(root, "tasks")
    os.makedirs(tasks_dir, exist_ok=True)
    # Ensure a config exists so subsequent run/report pick up defaults, but do
    # not clobber an existing tree.
    init_scaffold(start)
    task_path = os.path.join(tasks_dir, name)
    return add_task.scaffold(task_path, from_dir=from_dir)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scaffold .openbench/ for private-repo evaluations.",
    )
    parser.add_argument(
        "--task", metavar="NAME",
        help="create .openbench/tasks/NAME (wrapper over add_task)",
    )
    parser.add_argument(
        "--from", dest="from_dir", metavar="DIR",
        help="with --task: copy DIR into the new task's workspace/",
    )
    args = parser.parse_args(argv)

    if args.from_dir and not args.task:
        parser.error("--from requires --task")

    if args.task:
        try:
            task_path = init_task(args.task, from_dir=args.from_dir)
        except (FileExistsError, NotADirectoryError, ValueError, OSError) as exc:
            print(f"obench init: {exc}", file=sys.stderr)
            return 1
        print(f"Created task scaffold: {task_path}")
        print(
            "Next steps:\n"
            "  1. Fill in every TODO in instruction.md, checker.sh, and PROVENANCE.md.\n"
            "  2. Leave workspace/ unsolved; put the golden answer in solution/.\n"
            "  3. Put checker-owned oracle data in checker_data/ if needed.\n"
            "  4. Run: obench validate\n"
            "  5. Smoke: obench run --harness null --task "
            f"{os.path.basename(task_path)}"
        )
        return 0

    notes = init_scaffold()
    print("OpenBench private-eval scaffold (.openbench/):")
    for note in notes:
        print(f"  {note}")
    print()
    print("Next steps:")
    print("  1. Author a task:  obench init --task <name> --from <subdir>")
    print("  2. Validate:       obench validate")
    print("  3. Preflight:      obench doctor --harness <names>")
    print("  4. Run:            obench run --harness null --task <name>")
    print("  5. Report:         obench report")
    print("See docs/private-evals.md for the full private-repo on-ramp.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
