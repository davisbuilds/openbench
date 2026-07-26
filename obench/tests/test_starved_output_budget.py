import unittest
from obench import failure_class as fc

def _row(cap, **kw):
    r={"harness":"pi","completed":True,"checker_exit":1,"turns":114,
       "tokens_output":9000,"success":False,
       "sampling_observed":[{"max_completion_tokens":8192},{"max_completion_tokens":cap}]}
    r.update(kw); return r

class StarvedBudgetTests(unittest.TestCase):
    def test_one_token_budget_is_infra_not_wrong_answer(self):
        self.assertEqual(fc.classify_failure(_row(1)), "infra")

    def test_healthy_budget_still_scores_normally(self):
        self.assertEqual(fc.classify_failure(_row(4096)), "wrong_answer")

    def test_a_solve_is_never_downgraded(self):
        self.assertEqual(fc.classify_failure(_row(1, success=True, checker_exit=0)), "solved")

    def test_uncapped_model_never_trips_it(self):
        r=_row(1); r["sampling_observed"]=[{"max_completion_tokens":None}]
        self.assertEqual(fc.classify_failure(r), "wrong_answer")

    def test_stored_verdict_is_corrected_on_read(self):
        r=_row(1); r["failure_class"]="wrong_answer"
        self.assertEqual(fc.class_for_report(r), "infra")

    def test_stored_solve_is_not_corrected_on_read(self):
        r=_row(1, success=True, checker_exit=0); r["failure_class"]="solved"
        self.assertEqual(fc.class_for_report(r), "solved")
