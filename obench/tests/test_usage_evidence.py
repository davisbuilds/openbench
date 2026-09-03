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
    def test_proxy_measured_only_when_no_native_scalar_to_rank(self):
        # The proxy meter is what the consumers select ONLY as a fallback, when
        # no native token scalar is present. Then proxy_measured is honest.
        grade, eligible, reason = ue.matrix_usage_policy(
            None, proxy_measured=True, native_tokens_present=False)
        self.assertEqual(grade, ue.GRADE_PROXY_MEASURED)
        self.assertTrue(eligible)
        self.assertIsNone(reason)

    def test_native_scalar_present_grades_vendor_not_proxy(self):
        # Codex P1: stats.effective_tokens/input_tokens/output_tokens and
        # compare._measurement all prefer the adapter's native token fields and
        # consult the proxy only as a fallback. So when a proxied adapter ALSO
        # reports vendor tokens, the number that actually gets ranked is the
        # vendor's -- labeling the row proxy_measured would claim independent
        # proxy provenance for a vendor-reported figure.
        grade, eligible, reason = ue.matrix_usage_policy(
            "vendor_split", proxy_measured=True, native_tokens_present=True)
        self.assertEqual(grade, ue.GRADE_VENDOR_REPORTED)
        self.assertTrue(eligible)
        self.assertIsNone(reason)

    def test_native_estimate_present_is_estimated_even_with_proxy(self):
        # A native estimate scalar is selected before the proxy by the consumers,
        # and estimates are excluded from ranking -- so the proxy meter never gets
        # to stand in for it. Grade the source actually selected: estimated.
        grade, eligible, reason = ue.matrix_usage_policy(
            "estimated", proxy_measured=True, native_tokens_present=True)
        self.assertEqual(grade, ue.GRADE_ESTIMATED)
        self.assertFalse(eligible)
        self.assertEqual(reason, ue.EXCLUSION_USAGE_ESTIMATED)

    def test_vendor_split_without_proxy_is_vendor_reported_eligible(self):
        grade, eligible, reason = ue.matrix_usage_policy(
            "vendor_split", proxy_measured=False, native_tokens_present=True)
        self.assertEqual(grade, ue.GRADE_VENDOR_REPORTED)
        self.assertTrue(eligible)
        self.assertIsNone(reason)

    def test_estimated_is_graded_but_not_rankable(self):
        grade, eligible, reason = ue.matrix_usage_policy(
            "estimated", proxy_measured=False, native_tokens_present=True)
        self.assertEqual(grade, ue.GRADE_ESTIMATED)
        self.assertFalse(eligible)
        self.assertEqual(reason, ue.EXCLUSION_USAGE_ESTIMATED)

    def test_none_and_unmetered_are_unavailable(self):
        for basis in (None, "unmetered", "something-unknown"):
            grade, eligible, reason = ue.matrix_usage_policy(
                basis, proxy_measured=False, native_tokens_present=False)
            self.assertEqual(grade, ue.GRADE_UNAVAILABLE, basis)
            self.assertFalse(eligible, basis)
            self.assertEqual(reason, ue.EXCLUSION_USAGE_UNAVAILABLE, basis)

    def test_vendor_basis_without_a_scalar_or_proxy_is_unavailable(self):
        # A vendor_split basis flag but no native scalar and no proxy meter leaves
        # nothing for the consumers to rank -> unavailable, not a hollow
        # vendor_reported claim.
        grade, eligible, reason = ue.matrix_usage_policy(
            "vendor_split", proxy_measured=False, native_tokens_present=False)
        self.assertEqual(grade, ue.GRADE_UNAVAILABLE)
        self.assertFalse(eligible)
        self.assertEqual(reason, ue.EXCLUSION_USAGE_UNAVAILABLE)

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

    def test_proxied_row_with_native_vendor_tokens_grades_vendor_reported(self):
        # Regression for the Codex P1: a row where the proxy meter fired AND the
        # adapter reported native vendor tokens must not be stamped
        # proxy_measured, because the consumers rank the native number.
        row = {
            "token_basis": "vendor_split",
            "token_basis_proxy": "proxy_measured",
            "tokens": 1234,
        }
        run._grade_usage_evidence(row)
        self.assertEqual(row["usage_evidence_grade"], ue.GRADE_VENDOR_REPORTED)
        self.assertTrue(row["usage_ranking_eligible"])
        self.assertIsNone(row["usage_ranking_exclusion_reason"])

    def test_proxy_only_row_grades_proxy_measured(self):
        # No native scalar of any kind: the proxy meter is what gets ranked.
        row = {
            "token_basis": None,
            "token_basis_proxy": "proxy_measured",
            "tokens": None,
        }
        run._grade_usage_evidence(row)
        self.assertEqual(row["usage_evidence_grade"], ue.GRADE_PROXY_MEASURED)
        self.assertTrue(row["usage_ranking_eligible"])


if __name__ == "__main__":
    unittest.main()
