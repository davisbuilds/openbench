"""Missing required evidence stays excluded through every result consumer."""
import unittest
from obench import failure_class, matrix_queue, report


class RequiredEvidenceClassificationTests(unittest.TestCase):
    def row(self, checker_exit=0, **overrides):
        row = dict(harness='captured', model='model', task='task', trial=1,
                   completed=True, checker_exit=checker_exit, success=checker_exit == 0,
                   score=1.0 if checker_exit == 0 else 0.5, tokens=1000, turns=3,
                   workspace_changed=True, failure_class='infra',
                   evidence_required=True, evidence_status='failed')
        return dict(row, **overrides)

    def test_required_evidence_exclusion_precedes_checker_verdict_and_stored_class(self):
        for checker in (0, 1):
            for stored in ('infra', 'solved', 'wrong_answer', None):
                for status in ('missing', 'disabled', 'partial', 'failed', None):
                    with self.subTest(checker=checker, stored=stored, status=status):
                        row=self.row(checker, failure_class=stored, evidence_status=status)
                        self.assertEqual(failure_class.classify_failure(row), 'infra')
                        self.assertEqual(failure_class.class_for_report(row), 'infra')
                        self.assertEqual(failure_class.classify_failure_reason(row),
                                         'required_evidence_unavailable')
                        self.assertTrue(failure_class.is_excluded_from_solve_rate(row))
                        self.assertFalse(matrix_queue.cell_is_satisfied(row))
                        self.assertEqual(matrix_queue.row_failure_class(row), 'infra')

    def test_report_excludes_unusable_checker_pass_and_partial_score(self):
        for checker in (0,1):
            with self.subTest(checker=checker):
                row=self.row(checker)
                _,_,stats=report.aggregate([row])
                arm=stats[('captured','model')]
                self.assertEqual((arm['n'],arm['succ'],arm['scores']), (0,0,[]))
                self.assertEqual(arm['taxonomy']['infra'], 1)
                valid=dict(row,evidence_status='complete')
                _,_,stats=report.aggregate([valid])
                arm=stats[('captured','model')]
                self.assertEqual(arm['n'],1)
                self.assertEqual(arm['succ'],int(checker==0))
                self.assertEqual(arm['scores'],[valid['score']])
                self.assertTrue(matrix_queue.cell_is_satisfied(valid))

    def test_optional_and_legacy_evidence_keep_checker_owned_behavior(self):
        for required in (False, None):
            row=self.row(evidence_required=required)
            self.assertFalse(failure_class.is_excluded_from_solve_rate(row))
            self.assertTrue(matrix_queue.cell_is_satisfied(row))
        row=self.row()
        del row['evidence_required']
        self.assertFalse(failure_class.is_excluded_from_solve_rate(row))


if __name__=='__main__':
    unittest.main()
