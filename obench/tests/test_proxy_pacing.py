#!/usr/bin/env python3
"""The proxy must pace requests to hosts that punish bursts.

Forensics on 1,694 ledgered requests during the 2026-07-25/26 laguna storm:
requests spaced >=5s from the previous upstream call succeeded ~95% even
mid-storm; <1s gaps 429'd 97.9% of the time. OpenRouter sends no Retry-After
(live-probed), so the pacing has to live client-side, and the proxy is the one
place every harness's traffic already flows through.
"""

import threading
import time
import unittest

from obench import proxy as proxy_mod


class ProxyPacingTests(unittest.TestCase):
    def setUp(self):
        proxy_mod._PACE_LAST.clear()
        self._saved = dict(proxy_mod._PACE_HOSTS)
        proxy_mod._PACE_HOSTS.clear()
        proxy_mod._PACE_HOSTS["throttled.example"] = 0.3

    def tearDown(self):
        proxy_mod._PACE_HOSTS.clear()
        proxy_mod._PACE_HOSTS.update(self._saved)
        proxy_mod._PACE_LAST.clear()

    def test_back_to_back_calls_are_separated_by_the_gap(self):
        t0 = time.monotonic()
        proxy_mod._pace_upstream("throttled.example")
        proxy_mod._pace_upstream("throttled.example")
        self.assertGreaterEqual(time.monotonic() - t0, 0.3)

    def test_unlisted_hosts_are_not_delayed(self):
        t0 = time.monotonic()
        for _ in range(3):
            proxy_mod._pace_upstream("api.example.com")
        self.assertLess(time.monotonic() - t0, 0.05)

    def test_gap_is_shared_across_threads_not_per_thread(self):
        # The forensic finding is about AGGREGATE rate: concurrent cells
        # retrying in lockstep created the thundering herd. Two threads must
        # share one clock.
        stamps = []
        def hit():
            proxy_mod._pace_upstream("throttled.example")
            stamps.append(time.monotonic())
        threads = [threading.Thread(target=hit) for _ in range(3)]
        t0 = time.monotonic()
        for t in threads: t.start()
        for t in threads: t.join()
        stamps.sort()
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(all(g >= 0.28 for g in gaps), gaps)
        self.assertGreaterEqual(stamps[-1] - t0, 0.55)

    def test_openrouter_is_paced_by_default(self):
        self.assertIn("openrouter.ai", self._saved)
        self.assertGreaterEqual(self._saved["openrouter.ai"], 5.0)


if __name__ == "__main__":
    unittest.main()
