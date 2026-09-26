#!/usr/bin/env python3
"""Probe subprocess timeout behavior for the OpenBench docker-client wedge.

This is intentionally small and local-only. It exercises the classic case where
an immediate child leaves a grandchild holding stdout open after the child has
exited, plus a noisy child that keeps stdout readable. On the Python builds
checked for the wedge, both should raise TimeoutExpired near the requested
limit; if they do, the generic held-pipe theory is not sufficient to explain a
33+ minute docker client overrun. The durable fix in bench/docker_exec.py avoids
this class of risk by not relying on communicate(timeout=...) for docker runs.
"""

from __future__ import annotations

import argparse
import inspect
import os
import selectors
import signal
import subprocess
import sys
import time


class ProbeHung(RuntimeError):
    pass


CASES = {
    "grandchild-holds-stdout": [
        "bash", "-c",
        "(sleep 3600) & echo child-exit",
    ],
    "inherited-extra-fd": [
        "bash", "-c",
        "exec 3>&1; (sleep 3600 >&3) & exec 3>&-; echo child-exit",
    ],
    "continuous-output": [
        "bash", "-c",
        "while true; do echo tick; sleep 0.05; done",
    ],
}


def _kill_process_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.communicate(timeout=1)
    except (subprocess.TimeoutExpired, ProcessLookupError):
        pass


def run_case(name: str, cmd: list[str], timeout_s: float) -> bool:
    def alarm_handler(signum, frame):  # noqa: ARG001 - signal handler API
        raise ProbeHung

    start = time.monotonic()
    old_handler = signal.signal(signal.SIGALRM, alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, timeout_s + 5)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - start
        _kill_process_group(proc)
        stdout = exc.stdout or b""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        print(f"{name}: TimeoutExpired elapsed={elapsed:.3f}s stdout_prefix={stdout[:80]!r}")
        return elapsed <= timeout_s + 1.0
    except ProbeHung:
        elapsed = time.monotonic() - start
        _kill_process_group(proc)
        print(f"{name}: communicate hung past outer watchdog elapsed={elapsed:.3f}s")
        return False
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
    elapsed = time.monotonic() - start
    print(f"{name}: NO TIMEOUT elapsed={elapsed:.3f}s")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=1.0)
    args = parser.parse_args(argv)

    print(f"python={sys.version.split()[0]} executable={sys.executable}")
    print(f"subprocess.py={subprocess.__file__}")
    print(f"DefaultSelector={selectors.DefaultSelector.__name__} _PopenSelector={subprocess._PopenSelector.__name__}")
    src = inspect.getsource(subprocess.Popen._communicate)
    print("communicate_deadline_checks=", all(s in src for s in [
        "timeout = self._remaining_time(endtime)",
        "selector.select(timeout)",
        "self._check_timeout(endtime, orig_timeout, stdout, stderr)",
    ]))

    ok = True
    for name, cmd in CASES.items():
        ok = run_case(name, cmd, args.timeout) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
