#!/usr/bin/env python3
"""Tests for cell-coverage accounting in report.py.

Motivating incident (2026-07-24): rate-limited cells are correctly excluded from
solve-rate denominators, but they were never refilled. Because 429s hit
long-running cells hardest (measured 134s vs 64s median) and long cells are the
hard ones, ``solved/countable`` described the easy subset and overstated two arms
by ~15 points. Coverage makes that visible before a number is published.
"""

import unittest

from obench import report


def _row(task, trial, fc, success=False):
    return {"harness": "h", "model": "m", "task": task, "trial": trial,
            "failure_class": fc, "success": success}


class TestReportCoverage(unittest.TestCase):
    def test_full_coverage_when_every_cell_has_a_verdict(self):
        rows = [_row("t", 1, "solved", True), _row("t", 2, "wrong_answer"),
                _row("t", 3, "timeout")]
        arms, _tasks, stats = report.aggregate(rows)
        self.assertEqual(report.coverage(stats[arms[0]]), 1.0)
        self.assertEqual(report.coverage_warnings(stats, arms), [])

    def test_excluded_only_cell_counts_as_planned_but_unsatisfied(self):
        # trial 3 never produced a verdict: planned=3, satisfied=2 -> 67%.
        rows = [_row("t", 1, "solved", True), _row("t", 2, "wrong_answer"),
                _row("t", 3, "rate_limited")]
        arms, _tasks, stats = report.aggregate(rows)
        self.assertAlmostEqual(report.coverage(stats[arms[0]]), 2 / 3)
        warnings = report.coverage_warnings(stats, arms)
        self.assertEqual(len(warnings), 1)
        self.assertIn("1 of 3 planned cells", warnings[0])

    def test_retry_after_exclusion_restores_full_coverage(self):
        # A 429 followed by a successful retry of the SAME cell must not count
        # twice; coverage is over distinct (task, trial) cells, not rows.
        rows = [_row("t", 1, "rate_limited"), _row("t", 1, "solved", True)]
        arms, _tasks, stats = report.aggregate(rows)
        self.assertEqual(report.coverage(stats[arms[0]]), 1.0)
        self.assertEqual(report.coverage_warnings(stats, arms), [])

    def test_all_classes_excluded_from_solve_rate_reduce_coverage(self):
        for fc in ("infra", "rate_limited", "stalled"):
            with self.subTest(failure_class=fc):
                rows = [_row("t", 1, "solved", True), _row("t", 2, fc)]
                arms, _tasks, stats = report.aggregate(rows)
                self.assertAlmostEqual(report.coverage(stats[arms[0]]), 0.5,
                                       msg=f"{fc} should not satisfy a cell")

    def test_threshold_boundary_does_not_warn_at_exactly_95_percent(self):
        rows = [_row("t", i, "solved", True) for i in range(1, 20)]
        rows.append(_row("t", 20, "rate_limited"))
        arms, _tasks, stats = report.aggregate(rows)
        self.assertAlmostEqual(report.coverage(stats[arms[0]]), 0.95)
        self.assertEqual(report.coverage_warnings(stats, arms), [])

    def test_tables_surface_coverage_and_flag_incomplete_arms(self):
        rows = [_row("t", 1, "solved", True), _row("t", 2, "rate_limited")]
        arms, tasks, stats = report.aggregate(rows)
        for text in (report.format_table(arms, tasks, stats),
                     report.format_efficiency(arms, stats)):
            self.assertIn("cov", text)
            self.assertIn("50%!", text, "incomplete coverage must carry the ! flag")
            self.assertIn("COVERAGE:", text, "table must carry the refill warning")


if __name__ == "__main__":
    unittest.main()
