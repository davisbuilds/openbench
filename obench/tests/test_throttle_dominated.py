#!/usr/bin/env python3
"""A cell whose replies were mostly 429-killed is not a capability verdict.

Storm cells complete, emit some tokens, and get a real checker FAIL, so they
pass every row-level gate: failure_class=wrong_answer, error=None, turns=16.
wide25: 38 of 50 laguna cells were in this state (370 replies ending in
"429 ... temporarily rate-limited") while deepseek ran 0 of 50 clean -- the
published 6% compared a starved arm against a healthy one. Found only when
Matthew asked "did you actually check through logs"; the rows could not show it
because no field carried the signal. Now one does.
"""

import unittest

from obench import failure_class as fc


def _row(ok, throttled, **kw):
    row = {"harness": "pi", "completed": True, "checker_exit": 1,
           "turns": ok, "tokens_output": 1000, "success": False,
           "replies_ok": ok, "replies_throttled": throttled}
    row.update(kw)
    return row


class ThrottleDominatedTests(unittest.TestCase):
    def test_dominated_cell_is_rate_limited_not_wrong_answer(self):
        # The wide25 shape: a few replies got through, most died on 429s,
        # checker judged the scraps.
        self.assertEqual(fc.classify_failure(_row(ok=5, throttled=14)), "rate_limited")

    def test_minority_throttling_keeps_the_verdict(self):
        # Recovered-with-work stays a real wrong answer (the original
        # checker-owned-verdict case).
        self.assertEqual(fc.classify_failure(_row(ok=14, throttled=5)), "wrong_answer")

    def test_a_solve_is_never_reclassified(self):
        self.assertEqual(
            fc.classify_failure(_row(ok=3, throttled=10, success=True, checker_exit=0)),
            "solved")

    def test_rows_without_the_fields_are_untouched(self):
        row = _row(ok=5, throttled=14)
        row["replies_ok"] = row["replies_throttled"] = None
        self.assertEqual(fc.classify_failure(row), "wrong_answer")

    def test_stored_verdict_corrected_on_read(self):
        row = _row(ok=3, throttled=20, failure_class="wrong_answer")
        self.assertEqual(fc.class_for_report(row), "rate_limited")

    def test_stored_solve_not_corrected_on_read(self):
        row = _row(ok=3, throttled=20, success=True, checker_exit=0,
                   failure_class="solved")
        self.assertEqual(fc.class_for_report(row), "solved")

    def test_tie_is_not_dominated(self):
        self.assertEqual(fc.classify_failure(_row(ok=5, throttled=5)), "wrong_answer")
