#!/usr/bin/env python3
"""Unit tests for the extracted per-cell decision core (`decide_cell_outcome`).

The scheduling decision that the serial loop inlines -- satisfied vs re-queue vs
exhaust (retry budget vs wall cap) vs config-error -- is pulled out as a pure
function of the finished row plus the owning arm's state. Isolating it here is
what makes arm-level parallelism tractable: each worker calls this against the
`ArmState` it owns, so the decision logic never needs a lock. These tests pin the
exact behavior of the loop body (matrix_queue.py ~802-857) before it is rewired.
"""

import unittest

from obench import matrix_queue as mq


def _arm(budgets=None):
    return mq.ArmState("codex x m", budgets or {"rate_limited": 2, "infra": 2, "stalled": 1})


class DecideCellOutcomeTests(unittest.TestCase):
    def test_solved_row_is_satisfied(self):
        arm = _arm()
        d = mq.decide_cell_outcome(
            rc=0, row={"failure_class": "solved"}, run_id="c1", arm_state=arm,
            prior_attempt=0, result_attempt_count=0, cell_wall=0.0, max_cell_wall_s=None)
        self.assertEqual(d.action, "satisfied")
        self.assertIn("c1", arm.satisfied_cells)

    def test_wrong_answer_row_is_satisfied_a_real_verdict(self):
        # wrong_answer is NOT excluded from solve-rate -> it is a graded verdict,
        # so the cell is satisfied (done), never re-queued.
        arm = _arm()
        d = mq.decide_cell_outcome(
            rc=0, row={"failure_class": "wrong_answer"}, run_id="c1", arm_state=arm,
            prior_attempt=0, result_attempt_count=0, cell_wall=0.0, max_cell_wall_s=None)
        self.assertEqual(d.action, "satisfied")

    def test_missing_row_is_config_error(self):
        # runner exited without writing any row -> mis-wired arm, not a retry case
        arm = _arm()
        d = mq.decide_cell_outcome(
            rc=2, row=None, run_id="c1", arm_state=arm,
            prior_attempt=0, result_attempt_count=0, cell_wall=0.0, max_cell_wall_s=None)
        self.assertEqual(d.action, "config_error")

    def test_rate_limited_with_budget_requeues(self):
        arm = _arm()
        d = mq.decide_cell_outcome(
            rc=0, row={"failure_class": "rate_limited"}, run_id="c1", arm_state=arm,
            prior_attempt=1, result_attempt_count=1, cell_wall=1800.0, max_cell_wall_s=3600)
        self.assertEqual(d.action, "requeue")
        self.assertEqual(d.failure_class, "rate_limited")
        self.assertEqual(arm.consecutive_excluded, 1)

    def test_rate_limited_over_wall_cap_exhausts_despite_budget(self):
        # cumulative wall crossed the cap -> stop even though the retry budget
        # would still allow a re-queue (the throttle-timeout burn guard).
        arm = _arm()
        d = mq.decide_cell_outcome(
            rc=0, row={"failure_class": "rate_limited"}, run_id="c1", arm_state=arm,
            prior_attempt=1, result_attempt_count=1, cell_wall=3600.0, max_cell_wall_s=3600)
        self.assertEqual(d.action, "exhausted")
        self.assertIn("c1", arm.exhausted_cells)

    def test_rate_limited_budget_depleted_exhausts(self):
        arm = _arm({"rate_limited": 1})
        d = mq.decide_cell_outcome(
            rc=0, row={"failure_class": "rate_limited"}, run_id="c1", arm_state=arm,
            prior_attempt=2, result_attempt_count=2, cell_wall=100.0, max_cell_wall_s=None)
        self.assertEqual(d.action, "exhausted")
        self.assertIn("c1", arm.exhausted_cells)

    def test_new_retry_count_advances(self):
        arm = _arm()
        d = mq.decide_cell_outcome(
            rc=0, row={"failure_class": "rate_limited"}, run_id="c1", arm_state=arm,
            prior_attempt=1, result_attempt_count=0, cell_wall=0.0, max_cell_wall_s=None)
        self.assertEqual(d.new_retry_count, 2)  # max(prior_attempt+1, result_attempt_count)


if __name__ == "__main__":
    unittest.main()
