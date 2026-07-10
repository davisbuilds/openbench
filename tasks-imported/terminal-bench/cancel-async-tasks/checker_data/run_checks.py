#!/usr/bin/env python3
"""Stdlib port of Terminal-Bench 'cancel-async-tasks' tests/test_outputs.py.

Runs with cwd = a fresh copy of the task workspace, into which checker.sh has
copied test.py and the agent has (hopefully) written run.py. Drives test.py in
subprocesses exactly as the upstream pytest suite did, but without pytest so it
runs on the minimal openbench-harness image (python3 stdlib only).

Exit 0 iff every scenario passes; otherwise prints the first failure and exits 1.
"""
import os
import selectors
import signal
import subprocess
import sys
import time

PY = sys.executable or "python3"

TASK_SLEEP_SECONDS = 2
CLEANUP_SLEEP_SECONDS = 1
TIMING_SLACK_SECONDS = 12
CONCURRENT_RUN_TIMEOUT = TASK_SLEEP_SECONDS + CLEANUP_SLEEP_SECONDS + TIMING_SLACK_SECONDS
SERIAL_RUN_TIMEOUT = 2 * (TASK_SLEEP_SECONDS + CLEANUP_SLEEP_SECONDS) + TIMING_SLACK_SECONDS
CANCEL_READINESS_TIMEOUT = 30
CANCEL_EXIT_TIMEOUT = 20


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
    # 2 tasks, concurrency 2: both should overlap. Use a timeout derived from
    # one 2s task sleep plus one 1s cleanup sleep and generous host-load slack,
    # then verify overlap from output ordering rather than a fragile wall-clock
    # cutoff: both tasks must start before the first one finishes.
    try:
        res = run(2, 2, timeout=CONCURRENT_RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        fail(f"tasks did not run concurrently (timed out at {CONCURRENT_RUN_TIMEOUT}s)")
    if res.returncode != 0:
        fail(f"concurrent run exited {res.returncode}: {res.stderr.decode()[-500:]}")
    out = res.stdout.decode("utf-8")
    for token, want in (("Task started.", 2), ("Task finished.", 2), ("Cleaned up.", 2)):
        if out.count(token) != want:
            fail(f"concurrent run: expected {want}x '{token}', got {out.count(token)}")
    first_finish = out.find("Task finished.")
    first_start = out.find("Task started.")
    second_start = out.find("Task started.", first_start + len("Task started."))
    if first_start == -1 or second_start == -1 or first_finish == -1 or second_start > first_finish:
        fail("concurrent run: second task did not start before the first task finished")


def check_max_concurrent():
    # 2 tasks, concurrency 1: must serialize -> total >= 6s.
    start = time.monotonic()
    try:
        res = run(2, 1, timeout=SERIAL_RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        fail(f"max-concurrent run timed out at {SERIAL_RUN_TIMEOUT}s")
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
    # SIGINT (KeyboardInterrupt) only after the tasks that are allowed to run have
    # actually started. This avoids host-load-sensitive sleeps before signalling.
    expected_started_before_cancel = min(n_tasks, max_concurrent)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [PY, "-u", "test.py", "--n-tasks", str(n_tasks),
         "--max-concurrent", str(max_concurrent)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    stdout_chunks = []
    deadline = time.monotonic() + CANCEL_READINESS_TIMEOUT
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while b"".join(stdout_chunks).count(b"Task started.") < expected_started_before_cancel:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                fail(f"cancel run (n={n_tasks},mc={max_concurrent}) never became ready for SIGINT "
                     f"within {CANCEL_READINESS_TIMEOUT}s: expected "
                     f"{expected_started_before_cancel}x 'Task started.', got "
                     f"{b''.join(stdout_chunks).count(b'Task started.')}")
            events = selector.select(timeout=remaining)
            if not events:
                continue
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                proc.kill()
                fail(f"cancel run (n={n_tasks},mc={max_concurrent}) exited before SIGINT readiness: "
                     f"expected {expected_started_before_cancel}x 'Task started.', got "
                     f"{b''.join(stdout_chunks).count(b'Task started.')}")
            stdout_chunks.append(chunk)
    finally:
        selector.close()
    proc.send_signal(signal.SIGINT)
    try:
        remaining_stdout, _ = proc.communicate(timeout=CANCEL_EXIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        fail(f"cancel run (n={n_tasks},mc={max_concurrent}) did not exit after SIGINT")
    finally:
        if proc.poll() is None:
            proc.kill()
    out = (b"".join(stdout_chunks) + remaining_stdout).decode("utf-8")
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
