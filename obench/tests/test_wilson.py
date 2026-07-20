#!/usr/bin/env python3
"""Unit tests for the Wilson score interval."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from report import wilson_ci  # noqa: E402


class TestWilsonCI(unittest.TestCase):
    def test_four_of_five(self):
        lo, hi = wilson_ci(4, 5)
        self.assertAlmostEqual(lo, 0.376, places=2)
        self.assertAlmostEqual(hi, 0.964, places=2)

    def test_zero_of_five(self):
        lo, hi = wilson_ci(0, 5)
        self.assertEqual(lo, 0.0)
        # Upper bound is positive but well below 1 for a clean run of failures.
        self.assertGreater(hi, 0.0)
        self.assertLess(hi, 0.6)

    def test_five_of_five(self):
        lo, hi = wilson_ci(5, 5)
        # Lower bound leaves room for uncertainty; upper bound clamps to 1.
        self.assertGreater(lo, 0.4)
        self.assertLess(lo, 1.0)
        self.assertEqual(hi, 1.0)

    def test_n_zero_edge(self):
        # No observations -> full uncertainty, no division by zero.
        self.assertEqual(wilson_ci(0, 0), (0.0, 1.0))

    def test_bounds_are_ordered_and_clamped(self):
        for s, n in [(1, 3), (2, 7), (10, 10), (0, 1), (50, 100)]:
            lo, hi = wilson_ci(s, n)
            self.assertLessEqual(0.0, lo)
            self.assertLessEqual(lo, hi)
            self.assertLessEqual(hi, 1.0)


if __name__ == "__main__":
    unittest.main()
