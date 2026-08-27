#!/usr/bin/env python3
"""Tests for the `obench bridge` lifecycle helper (obench/bridge_cli.py).

Covers the pure/testable logic only: config-hash staleness detection, state
file read/write, the port-probe health notion (mirroring
adapters/codex.py:_bridge_reachable), and the up-is-noop-when-matching /
warn-when-stale orchestration decisions. Actually launching LiteLLM is out of
scope for unit tests -- see module docstring in bridge_cli.py.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from obench import bridge_cli


class ConfigHashTests(unittest.TestCase):
    def test_hash_is_stable_for_same_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("model_list: []\n")
            h1 = bridge_cli.config_hash(path)
            h2 = bridge_cli.config_hash(path)
            self.assertEqual(h1, h2)
            self.assertEqual(len(h1), 64)  # sha256 hex digest

    def test_hash_changes_when_content_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("model_list: []\n")
            before = bridge_cli.config_hash(path)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("model_list:\n  - model_name: new-model\n")
            after = bridge_cli.config_hash(path)
            self.assertNotEqual(before, after)


class StateFileTests(unittest.TestCase):
    def test_read_missing_state_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(bridge_cli.read_state(os.path.join(tmp, "nope.json")))

    def test_read_corrupt_state_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            self.assertIsNone(bridge_cli.read_state(path))

    def test_read_state_missing_pid_key_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"config_hash": "abc"}, fh)
            self.assertIsNone(bridge_cli.read_state(path))

    def test_write_then_read_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "state.json")
            bridge_cli.write_state(path, {"pid": 123, "config_hash": "abc"})
            got = bridge_cli.read_state(path)
            self.assertEqual(got["pid"], 123)
            self.assertEqual(got["config_hash"], "abc")

    def test_remove_state_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            bridge_cli.remove_state(path)  # missing file: must not raise
            bridge_cli.write_state(path, {"pid": 1})
            bridge_cli.remove_state(path)
            self.assertFalse(os.path.exists(path))
            bridge_cli.remove_state(path)  # already gone: must not raise


class PidAliveTests(unittest.TestCase):
    def test_current_process_is_alive(self):
        self.assertTrue(bridge_cli.pid_alive(os.getpid()))

    def test_none_and_zero_are_not_alive(self):
        self.assertFalse(bridge_cli.pid_alive(None))
        self.assertFalse(bridge_cli.pid_alive(0))
        self.assertFalse(bridge_cli.pid_alive(-1))

    def test_implausible_pid_is_not_alive(self):
        # A pid far beyond any plausible live process on a dev/CI box.
        self.assertFalse(bridge_cli.pid_alive(2**30))


class PortOpenTests(unittest.TestCase):
    def test_open_port_is_detected(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(bridge_cli.port_open("127.0.0.1", port, timeout=1.0))
        finally:
            srv.close()

    def test_closed_port_is_not_reachable(self):
        # Bind-then-close to get a port almost certainly unused, avoiding a
        # flaky hardcoded port number.
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()
        self.assertFalse(bridge_cli.port_open("127.0.0.1", port, timeout=0.5))


class WaitForPortTests(unittest.TestCase):
    def test_returns_true_immediately_when_already_open(self):
        calls = {"sleep": 0}
        with mock.patch.object(bridge_cli, "port_open", return_value=True):
            ok = bridge_cli.wait_for_port(
                "h", 1, timeout_total=5.0,
                _sleep=lambda s: calls.__setitem__("sleep", calls["sleep"] + 1),
                _clock=lambda: 0.0,
            )
        self.assertTrue(ok)
        self.assertEqual(calls["sleep"], 0)

    def test_polls_until_open_then_succeeds(self):
        # Reports closed twice, then open; a mock clock never reaches the
        # deadline so success comes only from the probe flipping to open.
        probe_results = iter([False, False, True])
        with mock.patch.object(bridge_cli, "port_open",
                                side_effect=lambda *a, **k: next(probe_results)):
            ok = bridge_cli.wait_for_port(
                "h", 1, timeout_total=100.0, interval=0.01,
                _sleep=lambda s: None, _clock=lambda: 0.0,
            )
        self.assertTrue(ok)

    def test_times_out_when_never_open(self):
        clock = {"t": 0.0}

        def fake_clock():
            return clock["t"]

        def fake_sleep(s):
            clock["t"] += 1.0

        with mock.patch.object(bridge_cli, "port_open", return_value=False):
            ok = bridge_cli.wait_for_port(
                "h", 1, timeout_total=3.0, interval=1.0,
                _sleep=fake_sleep, _clock=fake_clock,
            )
        self.assertFalse(ok)


class StalenessTests(unittest.TestCase):
    def test_none_state_is_never_stale(self):
        self.assertFalse(bridge_cli.is_stale(None, "any-hash"))

    def test_matching_hash_is_not_stale(self):
        self.assertFalse(bridge_cli.is_stale({"config_hash": "abc"}, "abc"))

    def test_mismatched_hash_is_stale(self):
        self.assertTrue(bridge_cli.is_stale({"config_hash": "abc"}, "xyz"))

    def test_missing_hash_key_is_stale(self):
        self.assertTrue(bridge_cli.is_stale({"pid": 1}, "xyz"))


class RunningStateTests(unittest.TestCase):
    def test_no_state_file_is_not_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            self.assertIsNone(bridge_cli.running_state(path, "h", 1))

    def test_dead_pid_is_not_running_even_if_port_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            bridge_cli.write_state(path, {"pid": 2**30, "config_hash": "abc"})
            with mock.patch.object(bridge_cli, "port_open", return_value=True):
                self.assertIsNone(bridge_cli.running_state(path, "h", 1))

    def test_alive_pid_but_closed_port_is_not_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            bridge_cli.write_state(path, {"pid": os.getpid(), "config_hash": "abc"})
            with mock.patch.object(bridge_cli, "port_open", return_value=False):
                self.assertIsNone(bridge_cli.running_state(path, "h", 1))

    def test_alive_pid_and_open_port_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            bridge_cli.write_state(path, {"pid": os.getpid(), "config_hash": "abc"})
            with mock.patch.object(bridge_cli, "port_open", return_value=True):
                state = bridge_cli.running_state(path, "h", 1)
            self.assertIsNotNone(state)
            self.assertEqual(state["pid"], os.getpid())


class CmdUpOrchestrationTests(unittest.TestCase):
    """`up` decision logic, with process launching/waiting mocked out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "openbench-home")
        self.config_path = os.path.join(self._tmp.name, "config.yaml")
        with open(self.config_path, "w", encoding="utf-8") as fh:
            fh.write("model_list: []\n")

        patches = [
            mock.patch.object(bridge_cli, "_openbench_home", return_value=self.home),
            mock.patch.object(bridge_cli, "_CONFIG_PATH", self.config_path),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _args(self, timeout=5.0):
        return argparse_namespace(timeout=timeout)

    def test_up_is_noop_when_already_up_with_matching_hash(self):
        current_hash = bridge_cli.config_hash(self.config_path)
        state_path = bridge_cli._state_path()
        bridge_cli.write_state(state_path, {
            "pid": os.getpid(), "config_hash": current_hash, "port": 4141,
        })
        with mock.patch.object(bridge_cli, "port_open", return_value=True), \
             mock.patch.object(bridge_cli, "subprocess") as mock_subprocess:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = bridge_cli.cmd_up(self._args())
        self.assertEqual(rc, 0)
        mock_subprocess.Popen.assert_not_called()
        self.assertIn("already up", stdout.getvalue())
        self.assertIn("config in sync", stdout.getvalue())

    def test_up_warns_loudly_when_running_with_stale_hash(self):
        state_path = bridge_cli._state_path()
        bridge_cli.write_state(state_path, {
            "pid": os.getpid(), "config_hash": "stale-hash-does-not-match", "port": 4141,
        })
        with mock.patch.object(bridge_cli, "port_open", return_value=True), \
             mock.patch.object(bridge_cli, "subprocess") as mock_subprocess:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = bridge_cli.cmd_up(self._args())
        self.assertEqual(rc, 1)
        mock_subprocess.Popen.assert_not_called()
        self.assertIn("STALE", stderr.getvalue())
        self.assertIn("obench bridge down", stderr.getvalue())

    def test_up_launches_and_records_state_when_not_running(self):
        fake_proc = mock.Mock(pid=99999)
        with mock.patch.object(bridge_cli, "port_open", return_value=True), \
             mock.patch.object(bridge_cli, "subprocess") as mock_subprocess:
            mock_subprocess.Popen.return_value = fake_proc
            mock_subprocess.STDOUT = -2
            rc = bridge_cli.cmd_up(self._args())
        self.assertEqual(rc, 0)
        mock_subprocess.Popen.assert_called_once()
        state = bridge_cli.read_state(bridge_cli._state_path())
        self.assertEqual(state["pid"], 99999)
        self.assertEqual(state["config_hash"], bridge_cli.config_hash(self.config_path))

    def test_up_reports_error_when_health_wait_times_out(self):
        fake_proc = mock.Mock(pid=99999)
        with mock.patch.object(bridge_cli, "port_open", return_value=False), \
             mock.patch.object(bridge_cli, "subprocess") as mock_subprocess, \
             mock.patch.object(bridge_cli, "wait_for_port", return_value=False):
            mock_subprocess.Popen.return_value = fake_proc
            mock_subprocess.STDOUT = -2
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = bridge_cli.cmd_up(self._args())
        self.assertEqual(rc, 1)
        self.assertIn("did not come up", stderr.getvalue())
        # No state recorded for a bridge that never became healthy.
        self.assertIsNone(bridge_cli.read_state(bridge_cli._state_path()))


class CmdDownTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "openbench-home")
        p = mock.patch.object(bridge_cli, "_openbench_home", return_value=self.home)
        p.start()
        self.addCleanup(p.stop)

    def test_down_with_no_state_and_closed_port_reports_not_running(self):
        with mock.patch.object(bridge_cli, "port_open", return_value=False):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = bridge_cli.cmd_down(argparse_namespace())
        self.assertEqual(rc, 0)
        self.assertIn("not running", stdout.getvalue())

    def test_down_clears_state_file(self):
        # pid_alive stubbed False so the pid-already-gone path is exercised
        # without signalling a real (unrelated) process.
        state_path = bridge_cli._state_path()
        bridge_cli.write_state(state_path, {"pid": os.getpid()})
        with mock.patch.object(bridge_cli, "pid_alive", return_value=False):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = bridge_cli.cmd_down(argparse_namespace())
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(state_path))

    def test_down_terminates_a_live_tracked_process(self):
        # Exercises the SIGTERM path against a real disposable child process
        # instead of mocking os.kill outright.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            state_path = bridge_cli._state_path()
            bridge_cli.write_state(state_path, {"pid": proc.pid})
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = bridge_cli.cmd_down(argparse_namespace())
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(state_path))
            proc.wait(timeout=5)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class CmdStatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "openbench-home")
        self.config_path = os.path.join(self._tmp.name, "config.yaml")
        with open(self.config_path, "w", encoding="utf-8") as fh:
            fh.write("model_list: []\n")
        patches = [
            mock.patch.object(bridge_cli, "_openbench_home", return_value=self.home),
            mock.patch.object(bridge_cli, "_CONFIG_PATH", self.config_path),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_status_down_when_port_closed(self):
        with mock.patch.object(bridge_cli, "port_open", return_value=False):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = bridge_cli.cmd_status(argparse_namespace())
        self.assertEqual(rc, 1)
        self.assertIn("DOWN", stdout.getvalue())

    def test_status_reports_in_sync_when_hash_matches(self):
        current_hash = bridge_cli.config_hash(self.config_path)
        bridge_cli.write_state(bridge_cli._state_path(), {
            "pid": os.getpid(), "config_hash": current_hash,
        })
        with mock.patch.object(bridge_cli, "port_open", return_value=True):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = bridge_cli.cmd_status(argparse_namespace())
        self.assertEqual(rc, 0)
        self.assertIn("UP", stdout.getvalue())
        self.assertIn("in sync", stdout.getvalue())

    def test_status_reports_stale_when_hash_differs(self):
        bridge_cli.write_state(bridge_cli._state_path(), {
            "pid": os.getpid(), "config_hash": "not-the-current-hash",
        })
        with mock.patch.object(bridge_cli, "port_open", return_value=True):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = bridge_cli.cmd_status(argparse_namespace())
        self.assertEqual(rc, 1)
        self.assertIn("STALE", stdout.getvalue())

    def test_status_untracked_process_reports_up_without_sync_claim(self):
        # Port open, but no state file at all (started outside our tool).
        with mock.patch.object(bridge_cli, "port_open", return_value=True):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = bridge_cli.cmd_status(argparse_namespace())
        self.assertEqual(rc, 0)
        self.assertIn("untracked", stdout.getvalue())


def argparse_namespace(**kwargs):
    import argparse
    return argparse.Namespace(**kwargs)


class MainDispatchTests(unittest.TestCase):
    def test_up_down_status_dispatch(self):
        with mock.patch.object(bridge_cli, "cmd_up", return_value=0) as up_mock:
            self.assertEqual(bridge_cli.main(["up"]), 0)
        up_mock.assert_called_once()

        with mock.patch.object(bridge_cli, "cmd_down", return_value=0) as down_mock:
            self.assertEqual(bridge_cli.main(["down"]), 0)
        down_mock.assert_called_once()

        with mock.patch.object(bridge_cli, "cmd_status", return_value=1) as status_mock:
            self.assertEqual(bridge_cli.main(["status"]), 1)
        status_mock.assert_called_once()

    def test_up_accepts_timeout_flag(self):
        captured = {}

        def fake_up(args):
            captured["timeout"] = args.timeout
            return 0

        with mock.patch.object(bridge_cli, "cmd_up", side_effect=fake_up):
            bridge_cli.main(["up", "--timeout", "12.5"])
        self.assertEqual(captured["timeout"], 12.5)

    def test_requires_a_subcommand(self):
        with self.assertRaises(SystemExit):
            bridge_cli.main([])


if __name__ == "__main__":
    unittest.main()
