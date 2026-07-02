#!/usr/bin/env python3
"""Benchmark runner core for the agent-harness comparison.

For each (task, harness, trial) cell this runner:
  1. copies ``tasks/<task>/workspace/`` to a disposable temp dir,
  2. dynamically imports the harness adapter and calls its ``run()`` per
     ADAPTER_SPEC.md (or uses the built-in ``null`` negative-control adapter),
  3. runs ``tasks/<task>/checker.sh`` with cwd=<temp dir> and env
     ``TASK_DIR=<absolute task dir>`` (checker exit 0 == task success),
  4. appends one JSON line describing the cell to the results log.

The loop is resumable: a cell whose ``run_id`` already appears in the results
log is skipped unless ``--force`` is given. Adapter exceptions are captured
into the row's ``error`` field rather than crashing the loop.

Python3 stdlib only. macOS-compatible (adapters enforce timeouts via
subprocess, never the ``timeout`` command).
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

DEFAULT_RESULTS_PATH = os.path.join(REPO, "results", "results.jsonl")
DEFAULT_ADAPTERS_DIR = os.path.join(HERE, "adapters")
DEFAULT_TASKS_DIR = os.path.join(REPO, "tasks")
DEFAULT_MODEL = "gpt-5.5-medium"

# Ordered field list for each results row.
ROW_FIELDS = (
    "run_id", "ts_iso", "harness", "model", "task", "trial",
    "success", "completed", "error", "wall_time_s", "tokens", "turns",
    "cmd", "checker_exit", "exec_mode",
)


def make_run_id(harness, task, model, trial):
    """Deterministic identity for a single benchmark cell."""
    return f"{harness}:{task}:{model}:trial{trial}"


def null_run(instruction, workdir, model, timeout_s):
    """Built-in negative-control adapter: does nothing, claims to complete.

    Because it never edits ``workdir``, the task checker must fail, so every
    cell run with ``--harness null`` should record ``success=false``.
    """
    return {
        "completed": True,
        "error": None,
        "output_tail": "",
        "tokens": None,
        "turns": None,
        "cmd": "null",
    }


def load_adapter(adapters_dir, name):
    """Dynamically import ``<adapters_dir>/<name>.py`` and return the module.

    The module must expose ``run(instruction, workdir, model, timeout_s)`` per
    ADAPTER_SPEC.md. Raises ``FileNotFoundError`` if the adapter file is absent.
    """
    path = os.path.join(adapters_dir, f"{name}.py")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"adapter not found: {path}")
    spec = importlib.util.spec_from_file_location(f"bench_adapter_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "run"):
        raise AttributeError(f"adapter '{name}' has no run() function")
    return module


def invoke_adapter(exec_mode, harness, instruction, workdir, model, timeout_s,
                   adapters_dir, docker_image, docker_fallback):
    """Run the harness for one cell, honoring the execution mode.

    Returns ``(result_dict, exec_used)`` where ``exec_used`` is ``"local"`` or
    ``"docker"``. In ``docker`` mode each cell runs in a fresh container (the
    same adapter, unchanged, via ``entry.py``); if the daemon or image is
    unavailable and ``docker_fallback`` is set, execution falls back to local
    and ``exec_used`` reflects that. The built-in ``null`` control runs the same
    way in either mode (its container path proves the plumbing without auth).
    """
    if exec_mode == "docker":
        import docker_exec  # lazy: local mode never needs docker
        try:
            result = docker_exec.run_in_container(
                harness, instruction, workdir, model, timeout_s,
                adapters_dir, docker_image,
            )
            return result, "docker"
        except docker_exec.DockerUnavailable as exc:
            if not docker_fallback:
                raise
            print(f"WARN docker unavailable ({exc}); falling back to local")

    if harness == "null":
        return null_run(instruction, workdir, model, timeout_s), "local"
    adapter = load_adapter(adapters_dir, harness)
    return adapter.run(instruction, workdir, model, timeout_s), "local"


def read_instruction(task_dir):
    """Return the contents of ``<task_dir>/instruction.md``."""
    with open(os.path.join(task_dir, "instruction.md"), encoding="utf-8") as fh:
        return fh.read()


def run_checker(task_dir, workdir, timeout_s):
    """Run ``<task_dir>/checker.sh`` with cwd=workdir and TASK_DIR set.

    Returns the checker's integer exit code, or the string ``"timeout"`` if the
    checker exceeds ``timeout_s`` seconds. The checker decides task success
    (exit 0 == success); the adapter never does.
    """
    checker = os.path.join(task_dir, "checker.sh")
    env = dict(os.environ)
    env["TASK_DIR"] = os.path.abspath(task_dir)
    try:
        proc = subprocess.run(
            ["bash", checker],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    return proc.returncode


def load_existing_run_ids(results_path):
    """Return the set of ``run_id`` values already present in the results log."""
    ids = set()
    if not os.path.isfile(results_path):
        return ids
    with open(results_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = row.get("run_id")
            if rid is not None:
                ids.add(rid)
    return ids


def append_row(results_path, row):
    """Append one JSON line (ordered by ROW_FIELDS) to the results log."""
    os.makedirs(os.path.dirname(os.path.abspath(results_path)), exist_ok=True)
    ordered = {key: row.get(key) for key in ROW_FIELDS}
    with open(results_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ordered) + "\n")


def run_cell(harness, task, model, trial, timeout_s, tasks_dir, adapters_dir,
             checker_timeout_s, exec_mode="local",
             docker_image=None, docker_fallback=True):
    """Execute one (task, harness, trial) cell and return its results row.

    Copies the task workspace to a temp dir, invokes the adapter (or the
    built-in null adapter), runs the checker, and cleans up. Adapter and
    checker failures are recorded in the row rather than raised. A checker that
    exceeds ``checker_timeout_s`` records ``checker_exit="timeout"``,
    ``success=false``.
    """
    run_id = make_run_id(harness, task, model, trial)
    # Absolute so the checker (run with cwd=temp workdir) and TASK_DIR resolve
    # correctly regardless of the caller's cwd or a relative --tasks-dir.
    task_dir = os.path.abspath(os.path.join(tasks_dir, task))
    row = {
        "run_id": run_id,
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "harness": harness,
        "model": model,
        "task": task,
        "trial": trial,
        "success": False,
        "completed": False,
        "error": None,
        "wall_time_s": None,
        "tokens": None,
        "turns": None,
        "cmd": None,
        "checker_exit": None,
        "exec_mode": None,
    }

    workdir = tempfile.mkdtemp(prefix=f"bench_{harness}_{task}_")
    try:
        # Copy the pristine workspace into the disposable temp dir. Never touch
        # the source workspace under tasks/.
        shutil.rmtree(workdir)
        shutil.copytree(os.path.join(task_dir, "workspace"), workdir)

        instruction = read_instruction(task_dir)

        start = time.monotonic()
        try:
            result, exec_used = invoke_adapter(
                exec_mode, harness, instruction, workdir, model, timeout_s,
                adapters_dir, docker_image, docker_fallback,
            )
        except Exception:  # noqa: BLE001 - never crash the loop on an adapter
            row["error"] = traceback.format_exc(limit=4).strip()
            row["wall_time_s"] = round(time.monotonic() - start, 3)
            row["exec_mode"] = exec_mode
            return row
        row["wall_time_s"] = round(time.monotonic() - start, 3)
        row["exec_mode"] = exec_used

        # Fold the adapter's self-reported fields into the row.
        row["completed"] = bool(result.get("completed", False))
        row["error"] = result.get("error")
        row["tokens"] = result.get("tokens")
        row["turns"] = result.get("turns")
        row["cmd"] = result.get("cmd")

        # The checker is the sole authority on task success.
        try:
            checker_exit = run_checker(task_dir, workdir, checker_timeout_s)
        except Exception:  # noqa: BLE001
            row["checker_exit"] = None
            if row["error"] is None:
                row["error"] = traceback.format_exc(limit=4).strip()
            return row
        row["checker_exit"] = checker_exit
        row["success"] = (checker_exit == 0)
        return row
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Agent-harness comparison runner.")
    parser.add_argument("--task", required=True,
                        help="comma-separated task name(s)")
    parser.add_argument("--harness", required=True,
                        help="comma-separated harness name(s), e.g. null,codex")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"canonical model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--trials", type=int, default=1,
                        help="trials per (task, harness) cell (default: 1)")
    parser.add_argument("--timeout", type=int, default=600,
                        help="per-cell adapter timeout in seconds (default: 600)")
    parser.add_argument("--checker-timeout", type=int, default=120,
                        help="checker.sh timeout in seconds (default: 120); "
                             "on timeout the row records checker_exit='timeout'")
    parser.add_argument("--force", action="store_true",
                        help="re-run cells even if their run_id already exists")
    parser.add_argument("--results-path", default=DEFAULT_RESULTS_PATH,
                        help="override the results.jsonl path")
    parser.add_argument("--adapters-dir", default=DEFAULT_ADAPTERS_DIR,
                        help="override the adapters directory")
    parser.add_argument("--tasks-dir", default=DEFAULT_TASKS_DIR,
                        help="override the tasks directory")
    parser.add_argument("--exec", dest="exec_mode", default="local",
                        choices=("local", "docker"),
                        help="execution backend: 'local' (host, default) or "
                             "'docker' (one disposable container per cell)")
    parser.add_argument("--docker-image", default="openbench-harness:latest",
                        help="image for --exec docker "
                             "(default: openbench-harness:latest)")
    parser.add_argument("--no-docker-fallback", dest="docker_fallback",
                        action="store_false",
                        help="in --exec docker, fail instead of falling back to "
                             "local when the daemon/image is unavailable")
    args = parser.parse_args(argv)

    tasks = [t.strip() for t in args.task.split(",") if t.strip()]
    harnesses = [h.strip() for h in args.harness.split(",") if h.strip()]

    existing = set() if args.force else load_existing_run_ids(args.results_path)

    ran = 0
    skipped = 0
    for harness in harnesses:
        for task in tasks:
            for trial in range(1, args.trials + 1):
                run_id = make_run_id(harness, task, args.model, trial)
                if run_id in existing:
                    skipped += 1
                    print(f"SKIP {run_id}")
                    continue
                row = run_cell(
                    harness, task, args.model, trial, args.timeout,
                    args.tasks_dir, args.adapters_dir, args.checker_timeout,
                    exec_mode=args.exec_mode, docker_image=args.docker_image,
                    docker_fallback=args.docker_fallback,
                )
                append_row(args.results_path, row)
                existing.add(run_id)
                ran += 1
                status = "ok" if row["success"] else "fail"
                print(f"RUN  {run_id} success={row['success']} "
                      f"completed={row['completed']} checker_exit={row['checker_exit']} "
                      f"exec={row['exec_mode']} [{status}]")

    print(f"\nDone. ran={ran} skipped={skipped} results={args.results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
