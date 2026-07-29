#!/usr/bin/env python3
"""Provider-pacing wait must be measured and excluded from latency.

The anti-throttle gate blocks each request until >=5s since the previous call
to a paced host, and that wait lands inside wall_time_s. Without measuring it,
paced runs read slower than unpaced ones for infrastructure reasons -- the
exact mixed-basis trap this benchmark keeps refusing to publish.
"""

import unittest

from obench import proxy as proxy_mod
from obench.report import aggregate
from obench.run import ROW_FIELDS, apply_proxy_ledger


class PacedWaitTests(unittest.TestCase):
    def test_pace_returns_time_waited(self):
        proxy_mod._PACE_LAST.clear()
        saved = dict(proxy_mod._PACE_HOSTS)
        proxy_mod._PACE_HOSTS.clear()
        proxy_mod._PACE_HOSTS["h.example"] = 0.2
        try:
            first = proxy_mod._pace_upstream("h.example")
            second = proxy_mod._pace_upstream("h.example")
            self.assertLess(first, 0.05)
            self.assertGreaterEqual(second, 0.15)
            self.assertEqual(proxy_mod._pace_upstream("other.example"), 0.0)
        finally:
            proxy_mod._PACE_HOSTS.clear()
            proxy_mod._PACE_HOSTS.update(saved)
            proxy_mod._PACE_LAST.clear()

    def test_ledger_wait_sums_into_the_row(self):
        row = {}
        ledger = [{"usage": {"input": 1, "output": 1}, "paced_wait_ms": 4000},
                  {"usage": {"input": 1, "output": 1}, "paced_wait_ms": 1500},
                  {"paced_wait_ms": 500}]  # non-usage requests still waited
        apply_proxy_ledger(row, ledger)
        self.assertEqual(row["paced_wait_s"], 6.0)
        self.assertIn("paced_wait_s", ROW_FIELDS)

    def test_report_latency_excludes_the_wait(self):
        rows = [{"harness": "pi", "model": "m", "task": "t", "trial": 1,
                 "success": True, "failure_class": "solved",
                 "wall_time_s": 100.0, "paced_wait_s": 40.0}]
        _, _, stats = aggregate(rows)
        self.assertEqual(stats[("pi", "m")]["wall_times"], [60.0])

    def test_unpaced_rows_are_untouched(self):
        rows = [{"harness": "pi", "model": "m", "task": "t", "trial": 1,
                 "success": True, "failure_class": "solved",
                 "wall_time_s": 100.0}]
        _, _, stats = aggregate(rows)
        self.assertEqual(stats[("pi", "m")]["wall_times"], [100.0])
