#!/usr/bin/env python3
"""Stdlib port of Terminal-Bench 'cancel-async-tasks' tests/test_outputs.py.

Runs with cwd = a fresh copy of the task workspace, into which checker.sh has
copied test.py and the agent has (hopefully) written run.py. Drives test.py in
subprocesses exactly as the upstream pytest suite did, but without pytest so it
runs on the minimal openbench-harness image (python3 stdlib only).

Exit 0 iff every scenario passes; otherwise prints the first failure and exits 1.
"""
import os
import signal
import subprocess
import sys
import time

PY = sys.executable or "python3"


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def run(n_tasks, max_concurrent, timeout):
    return subprocess.run(
        [PY, "test.py", "--n-tasks", str(n_tasks),
         "--max-concurrent", str(max_concurrent)],
        timeout=timeout, capture_output=True,
    )


def check_run_py_exists():
    if not os.path.exists("run.py"):
        fail("run.py does not exist")


def check_concurrent():
    # 2 tasks, concurrency 2: each task sleeps 2s + 1s cleanup; must finish < 5s.
    try:
        res = run(2, 2, timeout=5)
    except subprocess.TimeoutExpired:
        fail("tasks did not run concurrently (timed out at 5s)")
    if res.returncode != 0:
        fail(f"concurrent run exited {res.returncode}: {res.stderr.decode()[-500:]}")
    out = res.stdout.decode("utf-8")
    for token, want in (("Task started.", 2), ("Task finished.", 2), ("Cleaned up.", 2)):
        if out.count(token) != want:
            fail(f"concurrent run: expected {want}x '{token}', got {out.count(token)}")


def check_max_concurrent():
    # 2 tasks, concurrency 1: must serialize -> total >= 6s.
    start = time.monotonic()
    try:
        res = run(2, 1, timeout=10)
    except subprocess.TimeoutExpired:
        fail("max-concurrent run timed out at 10s")
    elapsed = time.monotonic() - start
    if res.returncode != 0:
        fail(f"max-concurrent run exited {res.returncode}: {res.stderr.decode()[-500:]}")
    out = res.stdout.decode("utf-8")
    for token, want in (("Task started.", 2), ("Task finished.", 2), ("Cleaned up.", 2)):
        if out.count(token) != want:
            fail(f"max-concurrent run: expected {want}x '{token}', got {out.count(token)}")
    if elapsed < 6:
        fail(f"max-concurrent run finished too fast ({elapsed:.2f}s < 6s); did not serialize")


def check_cancel(n_tasks, max_concurrent):
    # SIGINT (KeyboardInterrupt) 0.5s in: exactly the 2 started tasks must clean up.
    proc = subprocess.Popen(
        [PY, "test.py", "--n-tasks", str(n_tasks),
         "--max-concurrent", str(max_concurrent)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(0.5)
    proc.send_signal(signal.SIGINT)
    try:
        stdout, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        fail(f"cancel run (n={n_tasks},mc={max_concurrent}) did not exit after SIGINT")
    finally:
        if proc.poll() is None:
            proc.kill()
    out = stdout.decode("utf-8")
    if out.count("Task started.") != 2:
        fail(f"cancel run (n={n_tasks},mc={max_concurrent}): expected 2x 'Task started.', "
             f"got {out.count('Task started.')}")
    if out.count("Cleaned up.") != 2:
        fail(f"cancel run (n={n_tasks},mc={max_concurrent}): expected 2x 'Cleaned up.' "
             f"(cleanup must run on cancellation), got {out.count('Cleaned up.')}")


def main():
    check_run_py_exists()
    check_concurrent()
    check_max_concurrent()
    check_cancel(2, 3)   # below max_concurrent
    check_cancel(2, 2)   # at max_concurrent
    check_cancel(3, 2)   # above max_concurrent (queued tasks must not start)
    print("PASS: all cancel-async-tasks scenarios passed")
    sys.exit(0)


if __name__ == "__main__":
    main()
