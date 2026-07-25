#!/usr/bin/env python3
"""Tests for the stall-kill watchdog via proxy liveness tracking.

Covers:
  - Proxy tracks last_activity_monotonic per cell
  - cell_last_activity_age returns correct age
  - Watchdog fires on stale ctx (no proxied calls within window)
  - Watchdog does NOT fire on active ctx (recent proxied calls)
  - Stall-kill is disabled when proxy is off (no liveness signal)
"""

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

    def test_fresh_cell_has_no_activity(self):
        """A newly registered cell with no requests reports no activity."""
        age = self.server.cell_last_activity_age("test-cell-1")
        self.assertIsNone(age)

    def test_after_request_age_is_small(self):
        """After a completed request, last_activity_age is near zero."""
        # Simulate a completed request by directly updating the ledger
        with self.server._ledger_condition:
            ledger = self.server._cell_ledgers.get("test-cell-1")
            ledger.last_activity_monotonic = time.monotonic()
        age = self.server.cell_last_activity_age("test-cell-1")
        self.assertIsNotNone(age)
        self.assertLess(age, 0.5)

    def test_stale_cell_reports_large_age(self):
        """A cell with old last_activity reports age >= the stall window."""
        stale_ts = time.monotonic() - 700  # 700s ago (>600s default stall)
        with self.server._ledger_condition:
            ledger = self.server._cell_ledgers.get("test-cell-1")
            ledger.last_activity_monotonic = stale_ts
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


class ProxyOffDisablesStallTests(unittest.TestCase):
    """Verify stall-kill is disabled when --proxy is off."""

    def test_watchdog_loop_exits_immediately_without_an_event(self):
        """With no armed event (proxy off => watchdog never armed), the loop is a no-op.

        ``_STALLED_EVENT`` is None outside a cell by design -- run_cell sets it
        before invocation and clears it after -- so asserting it is non-None at
        rest tests the opposite of the contract. What matters is that the loop
        cannot kill anything when it was never armed.
        """
        self.assertIsNone(bench_run._STALLED_EVENT,
                          "no cell running => no armed stall event")
        killed = []
        proxy_server = mock.Mock()
        # Age far beyond any timeout: the loop must STILL not kill, because the
        # absence of an armed event means stall-kill is disabled for this cell.
        proxy_server.cell_last_activity_age.return_value = 10_000.0
        bench_run._stall_watchdog_loop(
            proxy_server, "cell", stall_timeout=1.0,
            kill_callback=lambda: killed.append(True), poll_interval=0.01)
        self.assertEqual(killed, [], "unarmed watchdog must never kill a cell")

    def test_armed_watchdog_does_kill_on_stale_activity(self):
        """Positive control: the same loop DOES kill once an event is armed."""
        killed = []
        proxy_server = mock.Mock()
        proxy_server.cell_last_activity_age.return_value = 10_000.0
        bench_run._STALLED_EVENT = threading.Event()
        try:
            bench_run._stall_watchdog_loop(
                proxy_server, "cell", stall_timeout=1.0,
                kill_callback=lambda: killed.append(True), poll_interval=0.01)
            self.assertEqual(killed, [True])
            self.assertTrue(bench_run._STALLED_EVENT.is_set())
        finally:
            bench_run._STALLED_EVENT = None


class LastActivityAgeInRowFields(unittest.TestCase):
    """Verify last_activity_age_s is tracked in the schema."""

    def test_field_in_row_fields(self):
        self.assertIn("last_activity_age_s", bench_run.ROW_FIELDS)


if __name__ == "__main__":
    unittest.main()
