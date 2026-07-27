#!/usr/bin/env python3
"""obench.report and obench.results_query must produce the same solve rate.

report.aggregate counted succ/n PER ROW while tracking coverage per cell, so a
retried cell inflated the denominator. On data/tb-mid-corrected.jsonl,
winning-avg-corewars#t2 had two attempts both classified wrong_answer, and the
two reporters disagreed on the same file: 5/19 = 26.3% versus 5/18 = 28%.

Retries are routine now that the queue re-runs excluded cells, so this was the
common case. Found by an adversarial review, not by the suite.
"""

import json
import os
import tempfile
import unittest

from obench.report import aggregate
from obench.results_query import _arm_cells, is_judged, load


def _write(rows):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def _row(task, trial, fc, success=False, ts=None):
    return {"harness": "pi", "model": "m", "task": task, "trial": trial,
            "run_id": f"pi:{task}:m:trial{trial}", "failure_class": fc,
            "success": success, "ts_iso": ts}


class ReportersAgreeTests(unittest.TestCase):
    def _both(self, rows):
        path = _write(rows)
        arms, _, stats = aggregate([json.loads(l) for l in open(path)])
        st = stats[arms[0]]
        cells = _arm_cells(load([path]))["pi x m"]
        judged = [r for r in cells.values() if is_judged(r)]
        return (st["succ"], st["n"]), (sum(1 for r in judged if r.get("success")),
                                       len(judged))

    def test_retried_cell_counts_once_in_both(self):
        # The exact live case: two attempts, both a real verdict.
        rows = [_row("t", 2, "wrong_answer", ts="2026-01-01"),
                _row("t", 2, "wrong_answer", ts="2026-01-02")]
        rep, rq = self._both(rows)
        self.assertEqual(rep, rq)
        self.assertEqual(rep[1], 1, "a retried cell must count once, not twice")

    def test_excluded_then_judged_counts_once(self):
        rows = [_row("t", 1, "rate_limited", ts="2026-01-01"),
                _row("t", 1, "solved", True, ts="2026-01-02")]
        rep, rq = self._both(rows)
        self.assertEqual(rep, rq)
        self.assertEqual(rep, (1, 1))

    def test_judged_beats_a_later_excluded_attempt_in_both(self):
        rows = [_row("t", 1, "wrong_answer", ts="2026-01-01"),
                _row("t", 1, "rate_limited", ts="2026-01-09")]
        rep, rq = self._both(rows)
        self.assertEqual(rep, rq)
        self.assertEqual(rep, (0, 1))

    def test_distinct_cells_are_not_collapsed(self):
        rows = [_row("t", 1, "solved", True), _row("t", 2, "wrong_answer"),
                _row("u", 1, "solved", True)]
        rep, rq = self._both(rows)
        self.assertEqual(rep, rq)
        self.assertEqual(rep, (2, 3))


if __name__ == "__main__":
    unittest.main()
