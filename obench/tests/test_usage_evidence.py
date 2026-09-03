#!/usr/bin/env python3
"""The matrix/legacy runner must grade its usage evidence, like the Harbor path.

``usage_evidence_grade`` fed a downstream data-honesty panel but was ``None`` on
every matrix-path row: the grade is computed only in the canonical Harbor path
(harbor_metering), never in run.py. The matrix runner's evidence vocabulary is
its own (the counting proxy's independent meter, the adapter's vendor token
split, or an estimate) -- distinct from Harbor-agent-reported usage -- so it
needs its own policy, and every produced row (failed cells included) must carry a
grade instead of None.
"""

import json
import os
import tempfile
import unittest

from obench import run
from obench import usage_evidence as ue


class MatrixUsagePolicyTests(unittest.TestCase):
    def test_proxy_measured_is_strongest_and_eligible(self):
        grade, eligible, reason = ue.matrix_usage_policy(
            "vendor_split", proxy_measured=True)
        self.assertEqual(grade, ue.GRADE_PROXY_MEASURED)
        self.assertTrue(eligible)
        self.assertIsNone(reason)

    def test_proxy_measured_wins_even_over_estimated_basis(self):
        # The independent proxy meter is authoritative regardless of the adapter's
        # own accounting quality.
        grade, eligible, _ = ue.matrix_usage_policy("estimated", proxy_measured=True)
        self.assertEqual(grade, ue.GRADE_PROXY_MEASURED)
        self.assertTrue(eligible)

    def test_vendor_split_without_proxy_is_vendor_reported_eligible(self):
        grade, eligible, reason = ue.matrix_usage_policy(
            "vendor_split", proxy_measured=False)
        self.assertEqual(grade, ue.GRADE_VENDOR_REPORTED)
        self.assertTrue(eligible)
        self.assertIsNone(reason)

    def test_estimated_is_graded_but_not_rankable(self):
        grade, eligible, reason = ue.matrix_usage_policy(
            "estimated", proxy_measured=False)
        self.assertEqual(grade, ue.GRADE_ESTIMATED)
        self.assertFalse(eligible)
        self.assertEqual(reason, ue.EXCLUSION_USAGE_ESTIMATED)

    def test_none_and_unmetered_are_unavailable(self):
        for basis in (None, "unmetered", "something-unknown"):
            grade, eligible, reason = ue.matrix_usage_policy(
                basis, proxy_measured=False)
            self.assertEqual(grade, ue.GRADE_UNAVAILABLE, basis)
            self.assertFalse(eligible, basis)
            self.assertEqual(reason, ue.EXCLUSION_USAGE_UNAVAILABLE, basis)

    def test_estimated_grade_fails_ranking_eligible_helper(self):
        # A consumer calling ranking_eligible() on the row must agree with the
        # explicit boolean the policy set.
        self.assertFalse(ue.ranking_eligible({"usage_evidence_grade": ue.GRADE_ESTIMATED}))


class RunCellGradesEveryRowTests(unittest.TestCase):
    def test_failed_null_cell_is_graded_usage_unavailable_not_none(self):
        d = tempfile.mkdtemp(prefix="usage_grade_e2e_")
        tasks = os.path.join(d, "tasks")
        os.makedirs(os.path.join(tasks, "t1"))
        with open(os.path.join(tasks, "t1", "instruction.md"), "w") as fh:
            fh.write("noop\n")
        with open(os.path.join(tasks, "t1", "checker.sh"), "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(tasks, "t1", "checker.sh"), 0o755)

        row = run.run_cell("null", "t1", "some-model", 1, 30, tasks, None, 30)
        self.assertIsNotNone(row["usage_evidence_grade"])
        self.assertIn(row["usage_evidence_grade"], ue.GRADES)
        # null harness reports no tokens -> unavailable, not rankable.
        self.assertEqual(row["usage_evidence_grade"], ue.GRADE_UNAVAILABLE)
        self.assertFalse(row["usage_ranking_eligible"])
        self.assertEqual(
            row["usage_ranking_exclusion_reason"], ue.EXCLUSION_USAGE_UNAVAILABLE)

        # And it survives serialization through ROW_FIELDS.
        out = os.path.join(d, "r.jsonl")
        run.append_row(out, row)
        with open(out) as fh:
            emitted = json.loads(fh.readline())
        self.assertEqual(emitted["usage_evidence_grade"], ue.GRADE_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
