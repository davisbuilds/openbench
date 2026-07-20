#!/usr/bin/env python3
"""Stress-repeat a task checker before admitting it to OpenBench.

This is the pre-admission determinism gate for a single task. It stages the
same workspace copies used by validate_tasks.py, then runs the checker many
consecutive times while optional CPU stress is active. A task passes only when
its golden solution is always accepted and its untouched workspace is always
rejected with the same failure signature.
"""

import argparse
import atexit
import json
import hashlib
import math
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

EXIT_FINDINGS = 3
DEFAULT_RUNS = 20
DEFAULT_STRESS = 6
DOCKER_IMAGE = "openbench-harness:latest"
DEFAULT_CHECKER_TIMEOUT_S = 600


def copy_tree(src, dst):
    """Copy the contents of src into dst (dst may already exist)."""
    if not os.path.isdir(src):
        return
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_root = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_root, exist_ok=True)
        for name in files:
            shutil.copy2(os.path.join(root, name), os.path.join(target_root, name))


def first_fail_line(output):
    """Return the first checker line containing FAIL, or the first nonblank line."""
    first_nonblank = ""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped and not first_nonblank:
            first_nonblank = stripped
        if "FAIL" in stripped.upper():
            return stripped
    return first_nonblank


def parse_score(output):
    """Return the last parseable SCORE value, clamped to [0.0, 1.0]."""
    score = None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("SCORE:"):
            try:
                value = float(stripped[len("SCORE:"):].strip())
            except ValueError:
                continue
            if not math.isfinite(value):
                continue
            score = max(0.0, min(1.0, value))
    return score


def effective_score(exit_code, parsed_score):
    if exit_code == 0:
        return 1.0
    if parsed_score is not None:
        return parsed_score
    return 0.0


def checker_env(task_dir, cwd):
    """Return a minimal checker environment without caller credentials."""
    env = {
        "TASK_DIR": task_dir,
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        "HOME": cwd,
        "TMPDIR": tempfile.gettempdir(),
    }
    for name in ("LANG", "LC_ALL", "LC_CTYPE"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def run_command(cmd, cwd=None, env=None, timeout=DEFAULT_CHECKER_TIMEOUT_S, docker_cidfile=None):
    """Run a checker command with process-group cleanup and timeout handling."""
    start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
        return proc.returncode, output, time.monotonic() - start, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if docker_cidfile and os.path.exists(docker_cidfile):
            try:
                with open(docker_cidfile, encoding="utf-8") as fh:
                    cid = fh.read().strip()
                if cid:
                    subprocess.run(["docker", "rm", "-f", cid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            except Exception:
                pass
        try:
            output, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = proc.communicate()
        output = (output or "") + f"\nFAIL: checker timed out after {timeout}s\n"
        return 124, output, time.monotonic() - start, True


def _burner_command():
    return [
        sys.executable,
        "-c",
        "import math\n"
        "x = 0.0\n"
        "while True:\n"
        "    x = math.sin(x + 1.23456789) * math.cos(x + 9.87654321)\n",
    ]


class StressBurners:
    """Spawn CPU burners in their own process groups and always reap them."""

    def __init__(self, count):
        self.count = max(0, int(count))
        self.processes = []
        self._installed = False
        self._old_handlers = {}

    def __enter__(self):
        if self.count <= 0:
            return self
        for _ in range(self.count):
            proc = subprocess.Popen(
                _burner_command(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.processes.append(proc)
        atexit.register(self.cleanup)
        self._install_signal_handlers()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.cleanup()
        self._restore_signal_handlers()
        return False

    def _install_signal_handlers(self):
        if self._installed:
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._old_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handle_signal)
            except (ValueError, OSError):
                pass
        self._installed = True

    def _restore_signal_handlers(self):
        for sig, handler in self._old_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        self._old_handlers.clear()
        self._installed = False

    def _handle_signal(self, signum, _frame):
        self.cleanup()
        old = self._old_handlers.get(signum)
        if callable(old):
            old(signum, _frame)
            return
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    def cleanup(self):
        for proc in self.processes:
            if proc.poll() is not None:
                continue
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.time() + 2.0
        for proc in self.processes:
            remaining = max(0.0, deadline - time.time())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for proc in self.processes:
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass


def _ignore_transient(_dir, names):
    return {name for name in names if name == "__pycache__" or name.endswith((".pyc", ".pyo"))}


@contextmanager
def staged_task_copy(task_dir):
    """Copy a task once so checker_data mutations are detectable but local-only."""
    tmp = tempfile.mkdtemp(prefix="detcheck-task-")
    try:
        task_copy = os.path.join(tmp, "task")
        shutil.copytree(task_dir, task_copy, ignore=_ignore_transient)
        yield task_copy
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def staged_workspace(task_dir, overlay_solution):
    tmp = tempfile.mkdtemp(prefix="detcheck-work-")
    try:
        copy_tree(os.path.join(task_dir, "workspace"), tmp)
        if overlay_solution:
            copy_tree(os.path.join(task_dir, "solution"), tmp)
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def checker_owned_snapshot(task_dir):
    """Return a content snapshot of checker-owned files under the staged task."""
    roots = [os.path.join(task_dir, "checker.sh"), os.path.join(task_dir, "checker_data")]
    snapshot = {}
    for root in roots:
        if os.path.isfile(root):
            candidates = [root]
        elif os.path.isdir(root):
            candidates = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [name for name in dirnames if name != "__pycache__"]
                for name in filenames:
                    if not name.endswith((".pyc", ".pyo")):
                        candidates.append(os.path.join(dirpath, name))
        else:
            candidates = []
        for path in candidates:
            rel = os.path.relpath(path, task_dir).replace(os.sep, "/")
            digest = hashlib.sha256()
            try:
                with open(path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                snapshot[rel] = f"ERROR:{exc}"
            else:
                snapshot[rel] = digest.hexdigest()
    return snapshot


def _run_checker_against_task(task_dir, overlay_solution, use_docker=False, timeout=DEFAULT_CHECKER_TIMEOUT_S):
    with staged_workspace(task_dir, overlay_solution) as workdir:
        docker_cidfile = None
        if use_docker:
            docker_cidfile = os.path.join(workdir, ".detcheck-container.cid")
            cmd = [
                "docker", "run", "--rm", "--cpus", "4", "--cidfile", docker_cidfile,
                "-v", f"{workdir}:/workspace",
                "-v", f"{task_dir}:/task:ro",
                "-w", "/workspace",
                "-e", "TASK_DIR=/task",
                DOCKER_IMAGE,
                "bash", "/task/checker.sh",
            ]
            exit_code, output, wall, timed_out = run_command(cmd, timeout=timeout, docker_cidfile=docker_cidfile)
        else:
            env = checker_env(task_dir, workdir)
            exit_code, output, wall, timed_out = run_command(
                ["bash", os.path.join(task_dir, "checker.sh")],
                cwd=workdir,
                env=env,
                timeout=timeout,
            )
    parsed_score = parse_score(output)
    return {
        "exit_code": exit_code,
        "ok": exit_code == 0,
        "score": effective_score(exit_code, parsed_score),
        "parsed_score": parsed_score,
        "score_errors": [],
        "first_fail_line": first_fail_line(output),
        "wall_time_s": wall,
        "timed_out": timed_out,
        "output": output,
    }


def run_checker_once(task_dir, overlay_solution, use_docker=False, timeout=DEFAULT_CHECKER_TIMEOUT_S, copy_task=True):
    """Run one checker invocation; copy the task unless caller provides a staged task."""
    task_dir = os.path.abspath(task_dir)
    if copy_task:
        with staged_task_copy(task_dir) as task_copy:
            return _run_checker_against_task(task_copy, overlay_solution, use_docker, timeout)
    return _run_checker_against_task(task_dir, overlay_solution, use_docker, timeout)


def wall_time_summary(verdicts):
    times = [v["wall_time_s"] for v in verdicts]
    if not times:
        return {"min": None, "median": None, "max": None}
    return {
        "min": min(times),
        "median": statistics.median(times),
        "max": max(times),
    }


def evaluate(solution_verdicts, workspace_verdicts):
    findings = []
    if not solution_verdicts or any(v["exit_code"] != 0 for v in solution_verdicts):
        findings.append("solution did not exit 0 on every run")
    if any(v.get("timed_out") for v in solution_verdicts):
        findings.append("solution checker timed out")
    solution_codes = {v["exit_code"] for v in solution_verdicts}
    solution_scores = {v["score"] for v in solution_verdicts}
    solution_parsed_scores = {v["parsed_score"] for v in solution_verdicts}
    if len(solution_codes) > 1:
        findings.append(f"solution exit codes diverged: {sorted(solution_codes)}")
    if len(solution_scores) > 1 or len(solution_parsed_scores) > 1:
        findings.append("solution scores diverged")
    if any(v["exit_code"] == 0 and v["parsed_score"] is not None and abs(v["parsed_score"] - 1.0) > 1e-9 for v in solution_verdicts):
        findings.append("solution exited 0 with SCORE other than 1.0")

    if any(v.get("timed_out") for v in workspace_verdicts):
        findings.append("workspace checker timed out")
    workspace_codes = {v["exit_code"] for v in workspace_verdicts}
    workspace_scores = {v["score"] for v in workspace_verdicts}
    workspace_fail_lines = {v["first_fail_line"] for v in workspace_verdicts}
    if not workspace_verdicts or any(v["exit_code"] == 0 for v in workspace_verdicts):
        findings.append("workspace did not fail on every run")
    if len(workspace_codes) > 1:
        findings.append(f"workspace exit codes diverged: {sorted(workspace_codes)}")
    if len(workspace_fail_lines) > 1:
        findings.append("workspace first FAIL lines diverged")
    if len(workspace_scores) > 1:
        findings.append(f"workspace scores diverged: {sorted(workspace_scores)}")
    return findings


def run_determinism_check(task_path, runs=DEFAULT_RUNS, stress=DEFAULT_STRESS, use_docker=False):
    task_dir = os.path.abspath(task_path)
    if runs < 1:
        raise ValueError("--runs must be >= 1")
    if stress < 0:
        raise ValueError("--stress must be >= 0")

    solution_verdicts = []
    workspace_verdicts = []
    workspace_runs = max(1, runs // 2)
    with staged_task_copy(task_dir) as task_copy, StressBurners(stress):
        baseline_snapshot = checker_owned_snapshot(task_copy)
        checker_mutations = []
        for idx in range(1, runs + 1):
            solution_verdicts.append(run_checker_once(task_copy, True, use_docker=use_docker, copy_task=False))
            current = checker_owned_snapshot(task_copy)
            if current != baseline_snapshot:
                checker_mutations.append(f"solution run {idx} mutated checker-owned files")
                baseline_snapshot = current
        for idx in range(1, workspace_runs + 1):
            workspace_verdicts.append(run_checker_once(task_copy, False, use_docker=use_docker, copy_task=False))
            current = checker_owned_snapshot(task_copy)
            if current != baseline_snapshot:
                checker_mutations.append(f"workspace run {idx} mutated checker-owned files")
                baseline_snapshot = current

    findings = evaluate(solution_verdicts, workspace_verdicts)
    if checker_mutations:
        findings.append("checker-owned files changed during determinism runs")
    all_verdicts = solution_verdicts + workspace_verdicts
    return {
        "task": task_dir,
        "runs": runs,
        "workspace_runs": workspace_runs,
        "stress": stress,
        "docker": use_docker,
        "solution_verdicts": solution_verdicts,
        "workspace_verdicts": workspace_verdicts,
        "wall_time_s": {
            "solution": wall_time_summary(solution_verdicts),
            "workspace": wall_time_summary(workspace_verdicts),
            "all": wall_time_summary(all_verdicts),
        },
        "findings": findings,
        "pass": not findings,
    }


def _short_output(text, limit=1200):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated] ...\n"


def print_human(result):
    status = "PASS" if result["pass"] else "FAIL"
    print(f"determinism_check: {status} {result['task']}")
    print(f"runs: solution={result['runs']} workspace={result['workspace_runs']} stress={result['stress']} docker={result['docker']}")
    for label in ("solution", "workspace", "all"):
        summary = result["wall_time_s"][label]
        print(
            f"wall_time_s.{label}: min={summary['min']:.3f} "
            f"median={summary['median']:.3f} max={summary['max']:.3f}"
            if summary["min"] is not None else f"wall_time_s.{label}: n/a"
        )
    if result["findings"]:
        print("findings:")
        for finding in result["findings"]:
            print(f"  - {finding}")
        print("\ndivergent outputs:")
        for label, verdicts in (("solution", result["solution_verdicts"]), ("workspace", result["workspace_verdicts"])):
            seen = set()
            for idx, verdict in enumerate(verdicts, 1):
                key = (verdict["exit_code"], verdict["first_fail_line"], verdict["output"])
                if key in seen:
                    continue
                seen.add(key)
                print(f"--- {label} run {idx}: exit={verdict['exit_code']} first_fail={verdict['first_fail_line']!r}")
                print(_short_output(verdict["output"]).rstrip())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_path")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--stress", type=int, default=DEFAULT_STRESS)
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = run_determinism_check(args.task_path, args.runs, args.stress, args.docker)
    except Exception as exc:  # CLI guard: make setup failures machine-readable too.
        result = {
            "task": os.path.abspath(args.task_path),
            "runs": args.runs,
            "stress": args.stress,
            "docker": args.docker,
            "solution_verdicts": [],
            "workspace_verdicts": [],
            "wall_time_s": {
                "solution": {"min": None, "median": None, "max": None},
                "workspace": {"min": None, "median": None, "max": None},
                "all": {"min": None, "median": None, "max": None},
            },
            "findings": [f"determinism_check crashed: {exc}"],
            "pass": False,
        }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print_human(result)
    return 0 if result["pass"] else EXIT_FINDINGS


if __name__ == "__main__":
    sys.exit(main())
