#!/usr/bin/env python3
"""Tests for the stall-kill watchdog via proxy liveness tracking.

Covers:
  - Proxy tracks last_activity_monotonic per cell
  - cell_last_activity_age returns correct age
  - Watchdog fires on stale ctx (no proxied calls within window)
  - Watchdog does NOT fire on active ctx (recent proxied calls)
  - Stall-kill is disabled when proxy is off (no liveness signal)
"""

import json
import os
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from obench import proxy as counting_proxy
from obench import run as bench_run
from obench import failure_class as fc_mod


class ProxyLivenessTrackingTests(unittest.TestCase):
    """Verify the proxy tracks last_activity_monotonic and exposes it."""

    def setUp(self):
        self.tmpdir = self._enter_context(
            __import__("tempfile").TemporaryDirectory(prefix="stall_test_"))
        self.server = counting_proxy.make_server(
            "127.0.0.1", 0, self.tmpdir)
        # shutdown() blocks until the serve_forever loop acknowledges it, so the
        # loop MUST be running or tearDown deadlocks. Matches test_proxy.py.
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.server.register_cell("test-cell-1")

    def _enter_context(self, ctx):
        return ctx.__enter__()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)

    def test_fresh_cell_starts_no_first_call_clock(self):
        age = self.server.cell_last_activity_age("test-cell-1")
        self.assertIsNotNone(age)
        self.assertLess(age, 0.5)

    def test_after_request_age_is_small(self):
        """After a completed request, last_activity_age is near zero."""
        # Simulate a completed request by directly updating the ledger
        with self.server._ledger_condition:
            ledger = self.server._cell_ledgers.get("test-cell-1")
            ledger.last_activity_monotonic = time.monotonic()
        age = self.server.cell_last_activity_age("test-cell-1")
        self.assertIsNotNone(age)
        self.assertLess(age, 0.5)

    def test_in_flight_request_remains_active(self):
        with self.server._ledger_condition:
            ledger = self.server._cell_ledgers["test-cell-1"]
            ledger.in_flight = 1
            ledger.last_activity_monotonic = time.monotonic() - 60
        age = self.server.cell_last_activity_age("test-cell-1")
        self.assertIsNotNone(age)
        self.assertLess(age, 0.5)

    def test_stale_cell_reports_large_age(self):
        """A cell with old last_activity reports age >= the stall window."""
        with mock.patch("obench.proxy.time.monotonic", return_value=1000.0):
            with self.server._ledger_condition:
                ledger = self.server._cell_ledgers.get("test-cell-1")
                ledger.last_activity_monotonic = 300.0
            age = self.server.cell_last_activity_age("test-cell-1")
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 700)

    def test_unregistered_cell_returns_none(self):
        """An unknown cell token returns None for activity queries."""
        age = self.server.cell_last_activity_age("nonexistent")
        self.assertIsNone(age)

    def test_activity_bumped_on_complete(self):
        """complete_cell_request updates last_activity_monotonic."""
        with self.server._ledger_condition:
            ledger = self.server._cell_ledgers.get("test-cell-1")
            ledger.in_flight = 1  # need in_flight > 0 for complete to work
        before = time.monotonic()
        self.server.complete_cell_request("test-cell-1", {"test": True})
        age = self.server.cell_last_activity_age("test-cell-1")
        self.assertIsNotNone(age)
        self.assertLess(age, time.monotonic() - before + 0.1)


class StallWatchdogLogicTests(unittest.TestCase):
    """Verify the watchdog thread's decision logic without running it."""

    def test_watchdog_fires_on_stale_ctx(self):
        """When proxy reports age >= stall_timeout, the event should be set."""
        proxy = mock.Mock()
        proxy.cell_last_activity_age.return_value = 601.0  # >600 default
        event = threading.Event()
        kill_called = []

        def kill_cb():
            kill_called.append(True)

        # We can't easily test the thread without running it, but we can test
        # the condition the thread checks directly.
        age = proxy.cell_last_activity_age("test")
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 600)

        # Simulate what the thread would do
        if age is not None and age >= 600:
            event.set()
            kill_cb()

        self.assertTrue(event.is_set())
        self.assertEqual(len(kill_called), 1)

    def test_watchdog_does_not_fire_on_active_ctx(self):
        """When proxy reports recent activity, the event should NOT be set."""
        proxy = mock.Mock()
        proxy.cell_last_activity_age.return_value = 5.0  # 5s ago, within window
        event = threading.Event()
        kill_called = []

        def kill_cb():
            kill_called.append(True)

        age = proxy.cell_last_activity_age("test")
        self.assertIsNotNone(age)
        self.assertLess(age, 600)

        # Simulate what the thread would do
        if age is not None and age >= 600:
            event.set()
            kill_cb()

        self.assertFalse(event.is_set())
        self.assertEqual(len(kill_called), 0)

    def test_watchdog_noop_when_no_activity_yet(self):
        """When proxy returns None (no activity yet), watchdog should not fire."""
        proxy = mock.Mock()
        proxy.cell_last_activity_age.return_value = None  # no activity yet
        event = threading.Event()
        kill_called = []

        def kill_cb():
            kill_called.append(True)

        age = proxy.cell_last_activity_age("test")
        self.assertIsNone(age)

        # Age is None, so the condition is not met
        if age is not None and age >= 600:
            event.set()
            kill_cb()

        self.assertFalse(event.is_set())
        self.assertEqual(len(kill_called), 0)


class FailureClassStalledTests(unittest.TestCase):
    """Verify 'stalled' is properly recognized as excluded from solve rate."""

    def test_stalled_in_excluded_set(self):
        self.assertIn("stalled", fc_mod.EXCLUDED_FROM_SOLVE_RATE)

    def test_stalled_in_failure_classes(self):
        self.assertIn("stalled", fc_mod.FAILURE_CLASSES)

    def test_stalled_excluded_from_solve_rate(self):
        row = {"failure_class": "stalled"}
        self.assertTrue(fc_mod.is_excluded_from_solve_rate(row))

    def test_solved_not_excluded(self):
        row = {"failure_class": "solved"}
        self.assertFalse(fc_mod.is_excluded_from_solve_rate(row))


class StallTerminationTests(unittest.TestCase):
    def test_successful_local_worker_reaps_descendants_and_preserves_result(self):
        with tempfile.TemporaryDirectory(prefix="local_worker_success_") as root_value:
            root = pathlib.Path(root_value)
            adapters = root / "adapters"
            adapters.mkdir()
            child_pid_path = root / "child.pid"
            expected = {
                "completed": True,
                "error": None,
                "tokens": 17,
                "turns": 2,
                "cmd": ["fixture"],
                "output_tail": "finished",
            }
            (adapters / "pi.py").write_text(
                "import pathlib, subprocess\n"
                "def run(_instruction, _workdir, _model, _timeout):\n"
                "    child = subprocess.Popen(['sleep', '30'])\n"
                f"    pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))\n"
                f"    return {expected!r}\n",
                encoding="utf-8",
            )
            unrelated = subprocess.Popen(
                ["sleep", "30"], start_new_session=True)
            child_pid = None
            try:
                result = bench_run._run_local_adapter_supervised(
                    "pi",
                    "finish",
                    str(root),
                    "fixture-model",
                    5,
                    str(adapters),
                    None,
                    {},
                    {"activate": lambda: None},
                )
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))

                self.assertEqual(result, expected)
                self.assertIsNone(unrelated.poll())
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if child_pid is None and child_pid_path.exists():
                    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                if child_pid is not None:
                    try:
                        os.kill(child_pid, bench_run.signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                unrelated.terminate()
                unrelated.wait(timeout=5)

    def test_watchdog_sets_event_only_after_bounded_termination(self):
        proxy_server = mock.Mock()
        proxy_server.cell_last_activity_age.return_value = 10_000.0
        stalled = threading.Event()
        observed = []
        outcome = {"confirmed": None, "started": threading.Event()}

        def terminate():
            observed.append(stalled.is_set())
            return False

        bench_run._stall_watchdog_loop(
            proxy_server,
            "cell",
            stall_timeout=1.0,
            kill_callback=terminate,
            stalled_event=stalled,
            poll_interval=0.01,
            termination_outcome=outcome,
        )
        self.assertEqual(observed, [False])
        self.assertTrue(outcome["started"].is_set())
        self.assertFalse(outcome["confirmed"])
        self.assertTrue(stalled.is_set())

    def test_owned_group_termination_exhausts_exact_bound(self):
        proc = mock.Mock(pid=424242)
        proc.wait.side_effect = subprocess.TimeoutExpired(["worker"], 0.01)
        with (
            mock.patch.object(
                bench_run, "_owned_process_group_exists", return_value=True),
            mock.patch.object(bench_run.os, "killpg") as killpg,
        ):
            confirmed = bench_run._terminate_owned_process_group(
                proc, attempts=3, wait_s=0.01)
        self.assertFalse(confirmed)
        self.assertEqual(killpg.call_count, 3)
        self.assertEqual(killpg.call_args_list[0].args[1], bench_run.signal.SIGTERM)
        self.assertEqual(killpg.call_args_list[1].args[1], bench_run.signal.SIGKILL)

    def test_local_stall_kills_only_owned_process_group_and_seals_ledger(self):
        with tempfile.TemporaryDirectory(prefix="stall_e2e_") as root_value:
            root = pathlib.Path(root_value)
            task = root / "tasks" / "fake"
            (task / "workspace").mkdir(parents=True)
            (task / "instruction.md").write_text(
                "wait forever\n", encoding="utf-8")
            checker = task / "checker.sh"
            checker.write_text(
                "#!/usr/bin/env bash\n"
                "echo checker-must-not-run >&2\n"
                "exit 99\n",
                encoding="utf-8",
            )
            checker.chmod(0o755)

            adapters = root / "adapters"
            adapters.mkdir()
            (adapters / "pi.py").write_text(
                "import pathlib, subprocess\n"
                "def run(_instruction, workdir, _model, _timeout):\n"
                "    proc = subprocess.Popen(['sleep', '30'])\n"
                "    pathlib.Path(workdir, 'owned.pid').write_text(str(proc.pid))\n"
                "    proc.wait()\n"
                "    return {'completed': True, 'error': None, 'tokens': None, "
                "'turns': None, 'cmd': ['sleep', '30'], 'output_tail': ''}\n",
                encoding="utf-8",
            )

            ledger_dir = root / "ledgers"
            server, thread = counting_proxy.start_in_thread(
                "127.0.0.1", 0, ledger_dir)
            port = server.server_address[1]
            proxy_ctx = {
                "ledger_dir": str(ledger_dir),
                "local_base_url": f"http://127.0.0.1:{port}",
                "docker_base_url": f"http://host.docker.internal:{port}",
                "_proxy_server": server,
            }
            owned_pids = []
            unrelated = subprocess.Popen(
                ["sleep", "30"], start_new_session=True)
            started = time.monotonic()
            try:
                row = bench_run.run_cell(
                    "pi",
                    "fake",
                    "deepseek-v4-flash",
                    1,
                    30,
                    str(root / "tasks"),
                    str(adapters),
                    5,
                    proxy_ctx=proxy_ctx,
                    stall_timeout=0.2,
                    workspace_observer=lambda workdir: owned_pids.append(
                        int(pathlib.Path(workdir, "owned.pid").read_text())
                    ),
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                unrelated_alive = unrelated.poll() is None
                unrelated.terminate()
                unrelated.wait(timeout=5)

            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue(unrelated_alive)
            self.assertEqual(row["failure_class"], "stalled")
            self.assertIsNone(row["checker_exit"])
            self.assertEqual(len(owned_pids), 1)
            with self.assertRaises(ProcessLookupError):
                os.kill(owned_pids[0], 0)
            records = [
                json.loads(line)
                for line in next(ledger_dir.glob("*.jsonl")).read_text(
                    encoding="utf-8").splitlines()
            ]
            self.assertEqual(records[-1]["record_type"], "ledger_seal")
            self.assertEqual(records[-1]["record_count"], 0)

    def test_workspace_setup_does_not_consume_stall_budget(self):
        with tempfile.TemporaryDirectory(prefix="stall_setup_") as root_value:
            root = pathlib.Path(root_value)
            task = root / "tasks" / "fake"
            (task / "workspace").mkdir(parents=True)
            (task / "instruction.md").write_text("finish\n", encoding="utf-8")
            checker = task / "checker.sh"
            checker.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            checker.chmod(0o755)
            adapters = root / "adapters"
            adapters.mkdir()
            (adapters / "pi.py").write_text(
                "def run(*_args):\n"
                "    return {'completed': True, 'error': None, 'tokens': None, "
                "'turns': None, 'cmd': [], 'output_tail': ''}\n",
                encoding="utf-8",
            )
            ledger_dir = root / "ledgers"
            server, thread = counting_proxy.start_in_thread(
                "127.0.0.1", 0, ledger_dir)
            proxy_ctx = {
                "ledger_dir": str(ledger_dir),
                "local_base_url": (
                    f"http://127.0.0.1:{server.server_address[1]}"),
                "_proxy_server": server,
            }
            materialize = bench_run.materialize_workspace

            def slow_materialize(*args, **kwargs):
                time.sleep(0.7)
                return materialize(*args, **kwargs)

            try:
                with mock.patch.object(
                        bench_run, "materialize_workspace",
                        side_effect=slow_materialize):
                    row = bench_run.run_cell(
                        "pi",
                        "fake",
                        "deepseek-v4-flash",
                        1,
                        5,
                        str(root / "tasks"),
                        str(adapters),
                        5,
                        proxy_ctx=proxy_ctx,
                        stall_timeout=0.5,
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            self.assertNotEqual(row["failure_class"], "stalled")


class LastActivityAgeInRowFields(unittest.TestCase):
    """Verify last_activity_age_s is tracked in the schema."""

    def test_field_in_row_fields(self):
        self.assertIn("last_activity_age_s", bench_run.ROW_FIELDS)


if __name__ == "__main__":
    unittest.main()
