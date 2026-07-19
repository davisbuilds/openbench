#!/usr/bin/env python3
import json
import os
import tempfile
import unittest

from bench import report_page


class ReportPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)

    def write(self, rows):
        path = os.path.join(self.tmp.name, "fixture.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows: fh.write(json.dumps(row) + "\n")
        return path

    @staticmethod
    def row(harness, trial, success, **extra):
        row = {"harness": harness, "model": "model-x", "task": "task", "trial": trial,
               "success": success, "score": int(success),
               "failure_class": "solved" if success else "wrong_answer",
               "wall_time_s": 10, "tokens_input_uncached": 100,
               "tokens_output": 20, "tokens_cache_read": 50,
               "token_basis": "vendor_split", "harness_version": "1", "timeout_s": 60}
        row.update(extra); return row

    def test_table_assembly_sorts_and_computes_timeout_columns(self):
        path = self.write([
            self.row("slow", 1, True, wall_time_s=20), self.row("slow", 2, False),
            self.row("fast", 1, True, wall_time_s=5),
            self.row("fast", 2, False, failure_class="timeout"),
        ])
        model = report_page.assemble_tables([{"path": path, "title": "Model X"}], tasks_dirs=[self.tmp.name])[0]
        self.assertEqual([a["arm"] for a in model["arms"]], ["fast", "slow"])
        self.assertTrue(model["has_timeouts"])
        fast = model["arms"][0]
        self.assertEqual((fast["solved"], fast["n"]), (1, 2))
        self.assertEqual(fast["tokens_input_uncached"], 200)
        self.assertEqual(fast["finished_rate"], 1.0)

    def test_harness_rates_use_matched_task_trial_cells(self):
        path = self.write([
            self.row("pi", 1, True), self.row("pi", 2, True),
            self.row("codex", 1, False),
        ])
        model = report_page.assemble_tables(
            [{"path": path, "matched": True}], tasks_dirs=[self.tmp.name]
        )[0]
        arms = {arm["arm"]: arm for arm in model["arms"]}
        self.assertEqual((arms["pi"]["solved"], arms["pi"]["n"]), (1, 1))
        self.assertEqual(arms["pi"]["unmatched"], 1)

    def test_disjoint_harness_cells_render_zero_denominators(self):
        path = self.write([self.row("pi", 1, True),
                           self.row("codex", 2, True)])
        model = report_page.assemble_tables(
            [{"path": path, "matched": True}], tasks_dirs=[self.tmp.name])[0]
        self.assertEqual({arm["n"] for arm in model["arms"]}, {0})
        self.assertIn("0/0", report_page.render_page([model], "Method"))

    def test_price_column_only_appears_for_a_priced_model(self):
        path = self.write([self.row("pi", 1, True)])
        unpriced = report_page.assemble_tables([{"path": path}])
        self.assertNotIn("$/solve", report_page.render_page(unpriced, "Method"))

        pricing = {"model-x": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}
        priced = report_page.assemble_tables([{"path": path}], pricing)
        page = report_page.render_page(priced, "Method")
        self.assertIn("$/solve", page)
        self.assertIn("$0.000", page)

    def test_html_snapshot_smoke_contains_expected_arms_values_and_no_external_assets(self):
        rows = [
            self.row("pi", 1, True, token_basis=None, token_basis_proxy="proxy_measured"),
            self.row("codex", 1, False),
        ]
        path = self.write(rows)
        models = report_page.assemble_tables([{"path": path}], tasks_dirs=[self.tmp.name])
        page = report_page.render_page(models, "- Honest limitation", "Snapshot")
        for expected in ("pi × model-x", "codex × model-x", "100.0%", "0.0%",
                         "proxy_measured", "Harness × model correctness",
                         "Methodology &amp; limitations"):
            self.assertIn(expected, page)
        self.assertNotIn("<script src=", page)
        self.assertNotIn("<link ", page)


if __name__ == "__main__": unittest.main()
