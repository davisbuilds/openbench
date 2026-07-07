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
import re
import shutil
import subprocess
import tempfile
import time
import traceback

from failure_class import classify_failure

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

DEFAULT_RESULTS_PATH = os.path.join(REPO, "results", "results.jsonl")
DEFAULT_ADAPTERS_DIR = os.path.join(HERE, "adapters")
DEFAULT_TASKS_DIR = os.path.join(REPO, "tasks")
DEFAULT_MODEL = "gpt-5.5-medium"

# Ordered field list for each results row. ``score`` and ``harness_version`` are
# appended last so older logs that predate them stay readable (report derives a
# score from ``success`` when the field is absent).
ROW_FIELDS = (
    "run_id", "ts_iso", "harness", "model", "task", "trial",
    "success", "completed", "error", "wall_time_s", "tokens", "turns",
    "cmd", "checker_exit", "exec_mode", "score", "harness_version",
    "failure_class",
)


def make_run_id(harness, task, model, trial):
    """Deterministic identity for a single benchmark cell."""
    return f"{harness}:{task}:{model}:trial{trial}"


def parse_score(stdout):
    """Return the partial-credit score from a checker's stdout, or None.

    A checker MAY print ``SCORE: <float>`` lines; the **last parseable** one
    wins. Values are clamped to [0.0, 1.0]. A malformed value (not a float) is
    ignored as if absent, so a trailing garbage line can't erase an earlier
    valid score. Returns None when no parseable SCORE line is present.
    """
    score = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("SCORE:"):
            continue
        try:
            val = float(line[len("SCORE:"):].strip())
        except ValueError:
            continue  # malformed -> treat this line as absent
        score = max(0.0, min(1.0, val))
    return score


def _extract_version(module):
    """Call a module's optional ``version() -> str | None`` defensively.

    Returns the string it yields, or None if the function is missing, returns a
    non-string, or raises. Never propagates an adapter's error.
    """
    fn = getattr(module, "version", None)
    if not callable(fn):
        return None
    try:
        v = fn()
    except Exception:  # noqa: BLE001 - a broken version() must not fail the run
        return None
    return v if isinstance(v, str) else None


def probe_version(harness, adapters_dir):
    """Best-effort harness version string for stamping into rows.

    The built-in ``null`` control reports ``"builtin"``. Real harnesses import
    their adapter and call its optional ``version()``; any failure yields None.
    Callers should cache this (one probe per harness per invocation).
    """
    if harness == "null":
        return "builtin"
    try:
        module = load_adapter(adapters_dir, harness)
    except Exception:  # noqa: BLE001
        return None
    return _extract_version(module)


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

    Returns ``(checker_exit, raw_score)`` where ``checker_exit`` is the integer
    exit code (or the string ``"timeout"``) and ``raw_score`` is the float from
    the checker's last parseable ``SCORE:`` line, or None if it printed none.
    The checker decides task success (exit 0 == success); the adapter never does.
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
        return "timeout", None
    return proc.returncode, parse_score(proc.stdout)


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


def default_transcripts_dir(results_path):
    """Base dir for transcripts: a ``transcripts/`` sibling of the results log.

    Co-locating transcripts with their results log keeps them together and
    means an ephemeral (temp) results path parks its transcripts in the same
    ephemeral tree -- so nothing leaks into the repo during tests. Override with
    ``--transcripts-dir``.
    """
    return os.path.join(os.path.dirname(os.path.abspath(results_path)),
                        "transcripts")


def transcript_path(transcripts_dir, results_stem, run_id):
    """Local path for a cell's transcript: <base>/<results-stem>/<run_id>.txt.

    ``run_id`` contains ``:`` separators; sanitize to a filesystem-safe token so
    the file name is portable and unambiguous.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", run_id)
    return os.path.join(transcripts_dir, results_stem, safe + ".txt")


def write_transcript(path, row, body):
    """Write one cell's full agent transcript to ``path`` (creating dirs).

    LOCAL-ONLY (user directive): transcripts are the raw, UNSCRUBBED harness
    output and may contain absolute home paths, usernames, hostnames, or leaked
    secrets. They are never published as-is -- run ``bench/scrub.py --check``
    for a manual review pass, then ``bench/scrub.py`` to emit scrubbed copies,
    before sharing any transcript. The runner writes originals here and builds
    no publishing path of any kind.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    header = (
        f"# transcript {row['run_id']}\n"
        f"# harness={row['harness']} model={row['model']} "
        f"task={row['task']} trial={row['trial']} ts={row['ts_iso']}\n"
        "# LOCAL-ONLY -- unscrubbed. Review with bench/scrub.py --check before sharing.\n\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header)
        fh.write(body or "")


def run_cell(harness, task, model, trial, timeout_s, tasks_dir, adapters_dir,
             checker_timeout_s, exec_mode="local",
             docker_image=None, docker_fallback=True, harness_version=None,
             transcripts_dir=None, results_stem=""):
    """Execute one (task, harness, trial) cell and return its results row.

    Copies the task workspace to a temp dir, invokes the adapter (or the
    built-in null adapter), runs the checker, and cleans up. Adapter and
    checker failures are recorded in the row rather than raised. A checker that
    exceeds ``checker_timeout_s`` records ``checker_exit="timeout"``,
    ``success=false``.

    Scoring: exit 0 => success=true, score=1.0 (a SCORE line can't lower a pass).
    Nonzero exit => success=false, score = the checker's SCORE line if any, else
    0.0. Timeout => score 0.0. ``harness_version`` (probed once per harness by
    the caller) is stamped verbatim.

    When ``transcripts_dir`` is set, the cell's full agent transcript
    (adapter ``full_output`` if present, else ``output_tail``) is persisted
    LOCAL-ONLY to ``<transcripts_dir>/<results_stem>/<run_id>.txt``. See
    ``write_transcript`` for the local-only handling rule.
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
        "output_tail": "",
        "checker_exit": None,
        "exec_mode": None,
        "score": 0.0,
        "harness_version": harness_version,
        "failure_class": None,
    }

    # Namespaced tasks (e.g. terminal-bench/feal) contain "/"; keep the prefix
    # a single path component.
    workdir = tempfile.mkdtemp(prefix=f"bench_{harness}_{task.replace('/', '_')}_")
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
            row["failure_class"] = classify_failure(row, "", timeout_s)
            return row
        row["wall_time_s"] = round(time.monotonic() - start, 3)
        row["exec_mode"] = exec_used

        # Fold the adapter's self-reported fields into the row.
        row["completed"] = bool(result.get("completed", False))
        row["error"] = result.get("error")
        row["tokens"] = result.get("tokens")
        row["turns"] = result.get("turns")
        row["cmd"] = result.get("cmd")
        row["output_tail"] = result.get("output_tail") or ""
        full_output = result.get("full_output")
        classifier_output = full_output if full_output is not None else row["output_tail"]

        # Persist the full agent transcript LOCAL-ONLY (prefer the untruncated
        # full_output; fall back to the ~2000-char output_tail). Never let a
        # transcript-write failure break the benchmark loop.
        if transcripts_dir:
            body = full_output
            if body is None:
                body = row["output_tail"]
            try:
                write_transcript(
                    transcript_path(transcripts_dir, results_stem, run_id),
                    row, body,
                )
            except Exception:  # noqa: BLE001 - transcript IO must not fail a cell
                pass

        # The checker is the sole authority on task success (and score).
        try:
            checker_exit, raw_score = run_checker(task_dir, workdir, checker_timeout_s)
        except Exception:  # noqa: BLE001
            row["checker_exit"] = None
            if row["error"] is None:
                row["error"] = traceback.format_exc(limit=4).strip()
            row["failure_class"] = classify_failure(row, classifier_output, timeout_s)
            return row
        row["checker_exit"] = checker_exit
        row["success"] = (checker_exit == 0)
        # exit 0 is a full pass (score 1.0) regardless of any SCORE line; a
        # nonzero exit takes the SCORE line for partial credit, else 0.0.
        row["score"] = 1.0 if checker_exit == 0 else (
            raw_score if raw_score is not None else 0.0)
        row["failure_class"] = classify_failure(row, classifier_output, timeout_s)
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
    parser.add_argument("--transcripts-dir", default=None,
                        help="base dir for LOCAL-ONLY per-cell transcripts "
                             "(default: a 'transcripts/' sibling of the results "
                             "log). Transcripts are unscrubbed; review with "
                             "bench/scrub.py --check before sharing.")
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

    transcripts_dir = args.transcripts_dir or default_transcripts_dir(args.results_path)
    results_stem = os.path.splitext(os.path.basename(args.results_path))[0]

    existing = set() if args.force else load_existing_run_ids(args.results_path)

    # Probe each harness's version at most once per invocation (a version()
    # probe may spawn a subprocess), then stamp the cached value into every row.
    versions = {h: probe_version(h, args.adapters_dir) for h in harnesses}

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
                    harness_version=versions.get(harness),
                    transcripts_dir=transcripts_dir, results_stem=results_stem,
                )
                append_row(args.results_path, row)
                existing.add(run_id)
                ran += 1
                status = "ok" if row["success"] else "fail"
                print(f"RUN  {run_id} success={row['success']} score={row['score']} "
                      f"completed={row['completed']} checker_exit={row['checker_exit']} "
                      f"exec={row['exec_mode']} [{status}]")

    print(f"\nDone. ran={ran} skipped={skipped} results={args.results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
