#!/usr/bin/env python3
"""A transient provider error must not erase a verdict the checker reached.

``classify_failure`` used to test for a rate-limit marker before anything else,
and the marker is matched against the WHOLE adapter transcript. So one 429
inside a long, successful-then-throttled run excluded a cell the checker had
already failed on its merits.

Campaign-wide that was 67 of 90 ``rate_limited`` and 11 of 85 ``infra`` cells:
78 real failures dropped from denominators, every one carrying a checker FAIL
message, median 11 turns and 15,885 output tokens, and not one a hidden solve.
Because exclusion tracked how often an arm got throttled, it inflated the
thin-capacity models most (laguna 44 cells, deepseek 13, kimi-k3 6, inkling 6,
against gpt-5.6's 5) -- flattering precisely the comparison being measured.

No existing test asserted that precedence either way, which is how it shipped.
"""

import unittest

from obench import failure_class as fc


def _row(**kw):
    row = {"harness": "pi", "completed": True, "checker_exit": 1,
           "turns": 11, "tokens_output": 15885, "success": False}
    row.update(kw)
    return row


RATE_LIMITED_TRANSCRIPT = (
    "... tool call ok ...\n"
    '{"error":"429 Too Many Requests: Provider returned error"}\n'
    "... retried, continued, finished ...\n"
)


class CheckerOwnedVerdictTests(unittest.TestCase):
    def test_recovered_rate_limit_keeps_the_checker_verdict(self):
        # The canonical case: agent hit a 429, recovered, finished; the checker
        # then FAILED the answer. That is a wrong answer, not an infra event.
        self.assertEqual(
            fc.classify_failure(_row(), RATE_LIMITED_TRANSCRIPT), "wrong_answer")

    def test_checker_pass_is_still_solved_not_reclassified(self):
        self.assertEqual(
            fc.classify_failure(_row(success=True, checker_exit=0),
                                RATE_LIMITED_TRANSCRIPT), "solved")

    def test_rate_limit_with_no_work_stays_excluded(self):
        # Throttled before producing anything: the model never answered, so it
        # cannot have answered wrong. This is the case the guards protect.
        self.assertEqual(
            fc.classify_failure(_row(turns=0, tokens_output=0),
                                RATE_LIMITED_TRANSCRIPT), "rate_limited")

    def test_incomplete_run_stays_excluded(self):
        # Killed mid-answer -> the agent never got to finish, so the checker's
        # verdict is about a truncated attempt, not the model's capability.
        self.assertEqual(
            fc.classify_failure(_row(completed=False), RATE_LIMITED_TRANSCRIPT),
            "rate_limited")

    def test_checker_crash_stays_excluded_even_when_completed(self):
        # exit outside {0,1} is a broken checker, never a model verdict. Which
        # infra-family label it lands on depends on pre-existing precedence
        # (a 429 marker is matched before the crash check), so assert the
        # property that matters for every published rate: it is NOT a verdict.
        for transcript in (RATE_LIMITED_TRANSCRIPT, "clean run\n"):
            got = fc.classify_failure(_row(checker_exit=125), transcript)
            self.assertIn(got, fc.EXCLUDED_FROM_SOLVE_RATE)
            self.assertNotEqual(got, "wrong_answer")

    def test_infra_marker_with_a_real_verdict_also_defers_to_the_checker(self):
        # Same reasoning for the infra family: 11 such cells existed.
        transcript = "docker: Error response from daemon: transient\nrecovered\n"
        self.assertEqual(fc.classify_failure(_row(), transcript), "wrong_answer")

    def test_no_marker_at_all_is_unchanged(self):
        self.assertEqual(fc.classify_failure(_row(), "clean run\n"), "wrong_answer")

    def test_predicate_requires_all_three_conditions(self):
        self.assertTrue(fc.has_checker_owned_verdict(_row()))
        self.assertFalse(fc.has_checker_owned_verdict(_row(completed=False)))
        self.assertFalse(fc.has_checker_owned_verdict(_row(checker_exit="timeout")))
        self.assertFalse(fc.has_checker_owned_verdict(_row(turns=0, tokens_output=0)))


class QueueSatisfactionTests(unittest.TestCase):
    """The queue must agree with the reporters about what counts as judged."""

    def test_stored_exclusion_with_a_real_verdict_counts_as_satisfied(self):
        from obench.matrix_queue import cell_is_satisfied
        row = _row(failure_class="rate_limited")
        self.assertTrue(
            cell_is_satisfied(row),
            "a cell whose checker already reached a verdict must not be "
            "re-queued; re-running it burns provider quota for no new data")

    def test_genuine_exclusion_is_still_requeued(self):
        from obench.matrix_queue import cell_is_satisfied
        row = _row(failure_class="rate_limited", turns=0, tokens_output=0)
        self.assertFalse(cell_is_satisfied(row))


if __name__ == "__main__":
    unittest.main()
