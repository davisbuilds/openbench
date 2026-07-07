#!/usr/bin/env python3
"""Tests for report.py solve-rate exclusion and taxonomy output."""

import os
import sys
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BENCH_DIR)

import report  # noqa: E402


class TestReportFailureClass(unittest.TestCase):
    def test_rate_limited_and_infra_excluded_from_denominator(self):
        rows = [
            {"harness": "h", "model": "m", "task": "t", "success": True,
             "failure_class": "solved", "tokens": 100},
            {"harness": "h", "model": "m", "task": "t", "success": False,
             "failure_class": "wrong_answer", "tokens": 50},
            {"harness": "h", "model": "m", "task": "t", "success": False,
             "failure_class": "rate_limited", "tokens": 1},
            {"harness": "h", "model": "m", "task": "t", "success": False,
             "failure_class": "infra", "tokens": 1},
        ]
        harnesses, tasks, stats = report.aggregate(rows)
        st = stats["h"]
        self.assertEqual(st["succ"], 1)
        self.assertEqual(st["n"], 2)
        self.assertEqual(st["per_task"]["t"], [1, 2])
        self.assertEqual(st["taxonomy"]["rate_limited"], 1)
        self.assertEqual(st["taxonomy"]["infra"], 1)

        table = report.format_table(harnesses, tasks, stats)
        self.assertIn("1/2 (50%)", table)
        self.assertEqual(report.tokens_per_solve(st), 150.0)

    def test_build_report_prints_taxonomy_per_harness_model(self):
        rows = [
            {"harness": "h", "model": "m1", "task": "t", "success": True,
             "failure_class": "solved"},
            {"harness": "h", "model": "m2", "task": "t", "success": False,
             "failure_class": "rate_limited"},
        ]
        harnesses, _tasks, stats = report.aggregate(rows)
        taxonomy = report.format_taxonomy(harnesses, stats)
        self.assertIn("harness", taxonomy)
        self.assertIn("rate_limited", taxonomy)
        self.assertTrue(any(line.startswith("h") and " m1 " in line for line in taxonomy.splitlines()))
        self.assertTrue(any(line.startswith("h") and " m2 " in line for line in taxonomy.splitlines()))


if __name__ == "__main__":
    unittest.main()
