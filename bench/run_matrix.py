#!/usr/bin/env python3
"""Expand and run an OpenBench benchmark matrix one cell at a time."""

import argparse
import os
import shlex
import signal
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run as bench_run  # noqa: E402

DEFAULT_OUT = bench_run.DEFAULT_RESULTS_PATH
DEFAULT_TASKS_DIR = bench_run.DEFAULT_TASKS_DIR
DEFAULT_RUNNER = os.path.join(HERE, "run.py")
TERMINATION_GRACE_SECONDS = 5

_STOP_AFTER_CURRENT = False
_INTERRUPT_COUNT = 0
_ACTIVE_PROC = None
_TERMINATE_AFTER_CURRENT = False
_TERMINATE_SIGNAL = None


def split_csv(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def expand_cells(harnesses, models, tasks, trials):
    cells = []
    for harness in harnesses:
        for model in models:
            for task in tasks:
                for trial in range(1, trials + 1):
                    cells.append({
                        "harness": harness,
                        "model": model,
                        "task": task,
                        "trial": trial,
                        "run_id": bench_run.make_run_id(harness, task, model, trial),
                    })
    return cells


def task_dir(tasks_dir, task):
    return os.path.abspath(os.path.join(tasks_dir, task))


def dropped_path(tasks_dir, task):
    return os.path.join(task_dir(tasks_dir, task), "DROPPED.md")


def assert_not_dropped(tasks_dir, tasks):
    dropped = [(task, dropped_path(tasks_dir, task)) for task in tasks if os.path.exists(dropped_path(tasks_dir, task))]
    if dropped:
        lines = [f"refusing dropped task {task!r}: {path}" for task, path in dropped]
        raise SystemExit("\n".join(lines))


def _report_files(data_dir):
    if not os.path.isdir(data_dir):
        return []
    names = []
    for name in os.listdir(data_dir):
        if name.startswith("admission-gate-report") and name.endswith(".md"):
            names.append(os.path.join(data_dir, name))
    return sorted(names)


def has_gate_pass_record(task, data_dir=None):
    """Best-effort lookup for a task admission PASS report.

    Historical gate reports are markdown under data/admission-gate-report*.md.
    The format is intentionally treated as advisory: callers warn on a miss
    rather than blocking benchmark scheduling.
    """
    data_dir = data_dir or os.path.join(REPO, "data")
    needles = {task, task.replace(os.sep, "/")}
    for path in _report_files(data_dir):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if any(needle in text for needle in needles) and "PASS" in text:
            return True
    return False


def warn_missing_gate_records(tasks, data_dir=None):
    for task in tasks:
        if not has_gate_pass_record(task, data_dir=data_dir):
            print(
                f"WARN: no admission gate PASS record found for task {task!r}; "
                "run 'python3 bench/admission_gate.py <path>' before relying on this matrix",
                file=sys.stderr,
            )


def runner_base_command(runner_cmd=None):
    override = runner_cmd or os.environ.get("OPENBENCH_RUNNER_CMD")
    if override:
        return shlex.split(override)
    return [sys.executable, DEFAULT_RUNNER]


def build_runner_command(cell, args):
    cmd = runner_base_command(args.runner_cmd)
    cmd.extend([
        "--harness", cell["harness"],
        "--model", cell["model"],
        "--task", cell["task"],
        "--trial", str(cell["trial"]),
        "--timeout", str(args.timeout),
        "--results-path", args.out,
        "--tasks-dir", args.tasks_dir,
    ])
    if args.docker:
        cmd.extend(["--exec", "docker"])
    return cmd


def _sigint_handler(_signum, _frame):
    global _STOP_AFTER_CURRENT, _INTERRUPT_COUNT
    _INTERRUPT_COUNT += 1
    _STOP_AFTER_CURRENT = True
    if _INTERRUPT_COUNT == 1:
        print("\nCtrl-C received: finishing the current cell, then stopping...", file=sys.stderr)
    else:
        print("\nCtrl-C already noted; still waiting for the current cell to finish cleanly.", file=sys.stderr)


def _termination_handler(signum, _frame):
    global _STOP_AFTER_CURRENT, _TERMINATE_AFTER_CURRENT, _TERMINATE_SIGNAL
    _STOP_AFTER_CURRENT = True
    _TERMINATE_AFTER_CURRENT = True
    _TERMINATE_SIGNAL = signum
    proc = _ACTIVE_PROC
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(proc.pid, signum)
        except ProcessLookupError:
            pass
    print(f"\nTermination signal {signum} received: stopping active cell...", file=sys.stderr)


def run_cell_subprocess(cmd):
    global _ACTIVE_PROC
    # Keep bench/run.py out of the terminal's SIGINT process group so Ctrl-C
    # reaches this wrapper, records a stop request, and lets the current cell
    # append its row. SIGHUP/SIGTERM handlers above still clean up this detached
    # process group so a dying wrapper does not orphan a costly agent run.
    proc = subprocess.Popen(cmd, start_new_session=True)
    _ACTIVE_PROC = proc
    try:
        while True:
            try:
                return proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                if not _TERMINATE_AFTER_CURRENT:
                    continue
                try:
                    return proc.wait(timeout=TERMINATION_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return proc.wait()
    finally:
        _ACTIVE_PROC = None


def run_matrix(args):
    global _STOP_AFTER_CURRENT, _INTERRUPT_COUNT, _TERMINATE_AFTER_CURRENT, _TERMINATE_SIGNAL
    _STOP_AFTER_CURRENT = False
    _INTERRUPT_COUNT = 0
    _TERMINATE_AFTER_CURRENT = False
    _TERMINATE_SIGNAL = None

    harnesses = split_csv(args.harness)
    models = split_csv(args.model)
    tasks = split_csv(args.task)
    if not harnesses or not models or not tasks:
        raise SystemExit("--harness, --model, and --task must each name at least one value")
    if args.trials < 1:
        raise SystemExit("--trials must be >= 1")

    assert_not_dropped(args.tasks_dir, tasks)
    if not args.skip_gate:
        warn_missing_gate_records(tasks)

    cells = expand_cells(harnesses, models, tasks, args.trials)
    existing = bench_run.load_existing_run_ids(args.out)

    if args.dry_run:
        for cell in cells:
            prefix = "SKIP" if cell["run_id"] in existing else "RUN "
            print(f"{prefix} {cell['run_id']}")
        print(f"dry-run: cells={len(cells)} runnable={sum(1 for c in cells if c['run_id'] not in existing)} skipped={sum(1 for c in cells if c['run_id'] in existing)}")
        return 0

    old_handlers = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGHUP: signal.getsignal(signal.SIGHUP),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }
    signal.signal(signal.SIGINT, _sigint_handler)
    signal.signal(signal.SIGHUP, _termination_handler)
    signal.signal(signal.SIGTERM, _termination_handler)
    ran = 0
    skipped = 0
    try:
        for cell in cells:
            if _STOP_AFTER_CURRENT:
                if _TERMINATE_AFTER_CURRENT:
                    return 128 + (_TERMINATE_SIGNAL or 0)
                print("Stopped before launching another cell due to Ctrl-C.", file=sys.stderr)
                break
            if cell["run_id"] in existing:
                skipped += 1
                print(f"SKIP {cell['run_id']}")
                continue
            print(f"CELL {cell['run_id']}", flush=True)
            code = run_cell_subprocess(build_runner_command(cell, args))
            existing = bench_run.load_existing_run_ids(args.out)
            wrote_expected_row = cell["run_id"] in existing
            if wrote_expected_row:
                ran += 1
            if _TERMINATE_AFTER_CURRENT:
                return 128 + (_TERMINATE_SIGNAL or 0)
            if code != 0:
                print(f"ERROR: runner exited {code} for {cell['run_id']}", file=sys.stderr)
                return code
            if not wrote_expected_row:
                print(f"ERROR: runner exited 0 but did not append {cell['run_id']}", file=sys.stderr)
                return 1
            if _STOP_AFTER_CURRENT:
                if _TERMINATE_AFTER_CURRENT:
                    return 128 + (_TERMINATE_SIGNAL or 0)
                print("Stopped after current cell due to Ctrl-C.", file=sys.stderr)
                break
        print(f"Done. ran={ran} skipped={skipped} results={args.out}")
        return 0
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True, help="comma-separated harnesses, e.g. pi,codex")
    parser.add_argument("--model", required=True, help="comma-separated model names")
    parser.add_argument("--task", required=True, help="comma-separated task names")
    parser.add_argument("--trials", type=int, required=True, help="trials per (harness, model, task)")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"output JSONL path (default: {DEFAULT_OUT})")
    parser.add_argument("--docker", action="store_true", help="run cells through bench/run.py --exec docker")
    parser.add_argument("--timeout", type=int, default=600, help="per-cell adapter timeout in seconds")
    parser.add_argument("--tasks-dir", default=DEFAULT_TASKS_DIR, help="task root directory")
    parser.add_argument("--skip-gate", action="store_true", help="skip admission-gate PASS record warning")
    parser.add_argument("--dry-run", action="store_true", help="print expanded cells and exit without launching run.py")
    parser.add_argument("--runner-cmd", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        return run_matrix(args)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(f"run_matrix: {exc.code}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    raise SystemExit(main())
