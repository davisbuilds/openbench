"""A checker that never ran its verifier carries no verdict.

The imported tb2 checkers delegate judging to a verifier container and print
``FAIL: verifier did not produce /logs/verifier/reward.txt (container exit N)``
when the reward file is missing -- docker exit 125 (image absent) or a /tmp
log mount resolving VM-local under colima. The checker then exits 1, which the
write path records as wrong_answer. class_for_report must reclassify such rows
as infra: the marker cannot appear on a genuinely judged run because the
verifier writes reward.txt for pass and fail alike.

Discovered 2026-07-29: all 12 tb2 checker images were missing on the benchmark
host, so 100% of tb2-tier wide25 rows (every arm) carried the marker and were
scored as capability failures.
"""
import unittest

from obench.failure_class import class_for_report, is_excluded_from_solve_rate


def _row(**over):
    row = {
        "failure_class": "wrong_answer",
        "success": False,
        "completed": True,
        "checker_exit": 1,
        "checker_stdout": "SCORE: 0\nFAIL: verifier did not produce "
                          "/logs/verifier/reward.txt (container exit 125)",
        "tokens": {"output": 5000},
    }
    row.update(over)
    return row


class CheckerVerdictlessTests(unittest.TestCase):
    def test_exit_125_row_reclassified_infra(self):
        self.assertEqual(class_for_report(_row()), "infra")
        self.assertTrue(is_excluded_from_solve_rate(_row()))

    def test_mount_loss_row_reclassified_infra(self):
        # container exit 0 but reward file vanished (colima /tmp mount): same
        # marker, same reclassification.
        row = _row(checker_stdout="SCORE: 0\nFAIL: verifier did not produce "
                                  "/logs/verifier/reward.txt (container exit 0)")
        self.assertEqual(class_for_report(row), "infra")

    def test_negative_control_genuine_fail_stays_wrong_answer(self):
        # A judged failure has a reward-derived SCORE line and no marker.
        row = _row(checker_stdout="2 failed, 1 passed\nSCORE: 0")
        self.assertEqual(class_for_report(row), "wrong_answer")

    def test_negative_control_solved_row_untouched(self):
        row = _row(failure_class="solved", success=True,
                   checker_exit=0, checker_stdout="SCORE: 1")
        self.assertEqual(class_for_report(row), "solved")

    def test_negative_control_excluded_row_not_double_corrected(self):
        # Already-excluded rows (e.g. rate_limited) keep their class even if
        # a checker marker is present; they are excluded either way.
        row = _row(failure_class="rate_limited")
        self.assertIn(class_for_report(row), ("rate_limited", "infra"))
        self.assertTrue(is_excluded_from_solve_rate(row))


if __name__ == "__main__":
    unittest.main()
