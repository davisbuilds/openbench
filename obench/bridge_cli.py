#!/usr/bin/env python3
"""Lifecycle helper for the open-model bridge (host-side LiteLLM proxy).

``obench/openmodel_bridge.sh`` starts the proxy open-model codex runs route
through (see that script's docstring for the why). It is written to run in
the foreground -- hand-started before a benchmark session and Ctrl-C'd after.
In practice it gets restarted roughly five times per session (each new model
route needs a config reload), and it is easy to accidentally run a whole
matrix against a bridge that was started before the last config edit.

This module wraps the script with ``obench bridge up|down|status``:

  - ``up``     starts the bridge in the background (a no-op if it is already
               up with a config hash matching the current
               ``obench/bridge/config.yaml``) and waits for the port to
               accept connections.
  - ``down``   stops the tracked bridge process and clears its state file.
  - ``status`` reports up/down, the port, and whether the running bridge's
               config hash matches the file on disk.

State (the bridge's pid and the sha256 of the config it was started with) is
recorded in a small JSON file under ``$OPENBENCH_HOME`` (default
``~/.openbench``) -- the same directory ``openmodel_bridge.sh`` already uses
for the litellm venv and provider keys.

The health notion (a plain TCP connect to the port) intentionally mirrors
``obench/adapters/codex.py:_bridge_reachable`` / ``_bridge_host`` -- the same
check the matrix runner relies on when it TCP-probes the bridge and returns
SETUP-NEEDED.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time

from .paths import PACKAGE_DIR

_BRIDGE_SCRIPT = os.path.join(PACKAGE_DIR, "openmodel_bridge.sh")
_CONFIG_PATH = os.path.join(PACKAGE_DIR, "bridge", "config.yaml")
_DEFAULT_PORT = 4141
# Matches adapters/codex.py's host-lane default (_bridge_host() with
# BENCH_IN_CONTAINER unset). The container-lane host.docker.internal
# indirection is the adapter's concern, not this process manager's.
_DEFAULT_HOST = "localhost"


def _openbench_home() -> str:
    return os.environ.get("OPENBENCH_HOME", os.path.expanduser("~/.openbench"))


def _state_path() -> str:
    return os.path.join(_openbench_home(), "bridge-state.json")


def _bridge_port() -> int:
    return int(os.environ.get("BENCH_BRIDGE_PORT", _DEFAULT_PORT))


def _bridge_host() -> str:
    return _DEFAULT_HOST


# --------------------------------------------------------------------------
# Pure / unit-testable helpers -- no process launching, no long waits by
# default. `up`/`down`/`status` below are thin orchestration over these.
# --------------------------------------------------------------------------

def config_hash(config_path: str) -> str:
    """sha256 hex digest of the bridge config file's raw bytes."""
    with open(config_path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def read_state(state_path: str) -> dict | None:
    """Parse the tracked pid/hash state file. None if missing or corrupt."""
    try:
        with open(state_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "pid" not in data:
        return None
    return data


def write_state(state_path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, state_path)


def remove_state(state_path: str) -> None:
    try:
        os.remove(state_path)
    except FileNotFoundError:
        pass


def pid_alive(pid: int | None) -> bool:
    """True if a process with this pid appears to exist (signal 0; no-op)."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone/something else
    return True


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Cheap TCP-connect probe -- the same health notion the matrix runner's
    adapter uses (adapters/codex.py:_bridge_reachable)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host: str, port: int, timeout_total: float = 30.0,
                   interval: float = 0.5, _sleep=time.sleep,
                   _clock=time.monotonic) -> bool:
    """Poll ``port_open`` until it succeeds or ``timeout_total`` elapses.

    ``_sleep``/``_clock`` are injectable so tests can exercise the polling
    and timeout logic without a real clock or a real listener.
    """
    deadline = _clock() + timeout_total
    while True:
        if port_open(host, port, timeout=min(interval, 2.0)):
            return True
        if _clock() >= deadline:
            return False
        _sleep(interval)


def is_stale(state: dict | None, current_hash: str) -> bool:
    """True iff a tracked bridge's recorded config hash no longer matches."""
    if state is None:
        return False
    return state.get("config_hash") != current_hash


def running_state(state_path: str, host: str, port: int) -> dict | None:
    """The tracked state dict IFF its pid is alive and the port answers;
    otherwise None (covers: never started, crashed, or orphaned state file
    left behind by an unclean shutdown)."""
    state = read_state(state_path)
    if state is None:
        return None
    if not pid_alive(state.get("pid")):
        return None
    if not port_open(host, port):
        return None
    return state


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def cmd_up(args) -> int:
    host, port = _bridge_host(), _bridge_port()
    state_path = _state_path()
    current_hash = config_hash(_CONFIG_PATH)

    running = running_state(state_path, host, port)
    if running is not None:
        if not is_stale(running, current_hash):
            print(f"bridge already up on {host}:{port} (config in sync) "
                  f"[pid {running['pid']}]")
            return 0
        print(f"WARNING: bridge is up on {host}:{port} but its config is "
              "STALE -- it was started before the last edit to "
              "obench/bridge/config.yaml.", file=sys.stderr)
        print("  run `obench bridge down` then `obench bridge up` to reload it.",
              file=sys.stderr)
        return 1

    # Not running (or the state file is stale/orphaned) -- clear bookkeeping
    # and launch a fresh instance in the background.
    remove_state(state_path)
    home = _openbench_home()
    os.makedirs(home, exist_ok=True)
    log_path = os.path.join(home, "bridge.log")
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [_BRIDGE_SCRIPT],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    print(f"starting bridge (pid {proc.pid}), waiting for {host}:{port}...")
    if not wait_for_port(host, port, timeout_total=args.timeout):
        print(f"ERROR: bridge did not come up on {host}:{port} within "
              f"{args.timeout}s -- see {log_path}", file=sys.stderr)
        return 1

    write_state(state_path, {
        "pid": proc.pid,
        "port": port,
        "config_hash": current_hash,
        "started_at": time.time(),
    })
    print(f"bridge up on {host}:{port} [pid {proc.pid}]")
    return 0


def cmd_down(args) -> int:
    host, port = _bridge_host(), _bridge_port()
    state_path = _state_path()
    state = read_state(state_path)

    if state is None:
        if port_open(host, port):
            print(f"WARNING: something is listening on {host}:{port} but "
                  "this tool has no tracked pid for it (started outside "
                  "`obench bridge up`?) -- leaving it running.",
                  file=sys.stderr)
            return 1
        print("bridge not running")
        return 0

    pid = state.get("pid")
    if pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        else:
            for _ in range(20):
                if not pid_alive(pid):
                    break
                time.sleep(0.25)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    remove_state(state_path)
    print(f"bridge stopped [pid {pid}]")
    return 0


def cmd_status(args) -> int:
    host, port = _bridge_host(), _bridge_port()
    state_path = _state_path()
    current_hash = config_hash(_CONFIG_PATH)
    state = read_state(state_path)
    up = port_open(host, port)

    if not up:
        print(f"bridge: DOWN ({host}:{port})")
        if state is not None:
            # Nothing is listening despite a tracked state file -- it crashed
            # or was killed outside our tools. Clear the orphaned bookkeeping.
            remove_state(state_path)
        return 1

    if state is not None and pid_alive(state.get("pid")):
        stale = is_stale(state, current_hash)
        sync = ("STALE CONFIG -- restart needed (obench bridge down && up)"
                 if stale else "in sync")
        print(f"bridge: UP ({host}:{port}) [pid {state['pid']}] config: {sync}")
        return 1 if stale else 0

    print(f"bridge: UP ({host}:{port}) [untracked process -- config hash unknown]")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="obench bridge",
        description="Manage the host-side open-model bridge (LiteLLM proxy) lifecycle.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser(
        "up", help="start the bridge (no-op if already up with matching config)")
    up.add_argument(
        "--timeout", type=float, default=30.0,
        help="seconds to wait for the bridge to become healthy (default: 30)")

    sub.add_parser("down", help="stop the bridge")
    sub.add_parser("status", help="report up/down and config-hash sync state")

    args = parser.parse_args(argv)
    if args.command == "up":
        return cmd_up(args)
    if args.command == "down":
        return cmd_down(args)
    if args.command == "status":
        return cmd_status(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
