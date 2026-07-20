#!/usr/bin/env python3
import html
import json
import os
import re
import stat
import tempfile
import unittest
from unittest import mock

from obench import report_page


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
        self.assertEqual(fast["total_tokens"], 340)
        self.assertEqual(fast["finished_rate"], 1.0)

    def test_timeout_columns_require_an_arm_above_five_percent(self):
        cases = ((0, False), (1, False), (2, True))
        for timeout_count, material in cases:
            with self.subTest(timeout_count=timeout_count):
                rows = []
                for trial in range(1, 21):
                    timed_out = trial <= timeout_count
                    rows.append(self.row("codex", trial, not timed_out,
                                         failure_class="timeout" if timed_out else "solved"))
                path = self.write(rows)
                model = report_page.assemble_tables(
                    [{"path": path}], tasks_dirs=[self.tmp.name])[0]
                self.assertEqual(model["material_timeouts"], material)
                page = report_page.render_page([model], "Method")
                self.assertEqual("Solve rate @cap" in page, material)
                self.assertEqual("Solve rate finished" in page, material)
                self.assertEqual("Solve rate</th>" in page, not material)
                if timeout_count == 1:
                    self.assertIn("codex: 1 timeout; finished-basis 100.0%", page)

    def test_total_tokens_column_and_timeout_label_suppression(self):
        path = self.write([
            self.row("pi", 1, True, tokens_input_uncached=120,
                     tokens_output=30, tokens_cache_read=50),
            self.row("pi", 2, False, tokens_input_uncached=80,
                     tokens_output=10, tokens_cache_read=10),
        ])
        model = report_page.assemble_tables([{"path": path}], tasks_dirs=[self.tmp.name])[0]
        self.assertEqual(model["arms"][0]["total_tokens"], 300)
        page = report_page.render_page([model], "Method")
        self.assertIn("Total tokens/solve", page)
        self.assertIn(">300</td>", page)
        self.assertLess(page.index("Total tokens/solve"), page.index("Uncached in/solve"))
        self.assertIn(html.escape(report_page.TOKEN_NOTE), page)
        self.assertIn("Solve rate</th>", page)
        self.assertNotIn("@cap", page)
        self.assertNotIn("Solve rate finished", page)

    def test_timeout_dataset_keeps_cap_and_finished_labels(self):
        path = self.write([
            self.row("pi", 1, True),
            self.row("pi", 2, False, failure_class="timeout"),
        ])
        models = report_page.assemble_tables([{"path": path}], tasks_dirs=[self.tmp.name])
        page = report_page.render_page(models, "Method")
        self.assertIn("Solve rate @cap", page)
        self.assertIn("Solve rate finished", page)

    def test_chart_data_and_svg_include_labels_points_and_cli_marker(self):
        path = self.write([
            self.row("cursor", 1, True, wall_time_s=8),
            self.row("cursor", 2, False, wall_time_s=12),
            self.row("pi", 1, True, wall_time_s=4),
            self.row("pi", 2, True, wall_time_s=6),
        ])
        model = report_page.assemble_tables([{"path": path}], tasks_dirs=[self.tmp.name])[0]
        chart_data = {row["arm"]: row for row in report_page._chart_data(model)}
        self.assertTrue(chart_data["cursor"]["cli_basis"])
        self.assertEqual(chart_data["pi"]["total_tokens"], 170)
        page = report_page.render_page([model], "Method")
        self.assertEqual(page.count('role="img"'), 3)
        for expected in ("Correctness by harness", "Total tokens / solve (log scale)",
                         "Median wall time", "cursor", "pi", "self-reported",
                         'data-arm="cursor"', 'data-arm="pi"', "<circle", "<path d=\"M "):
            self.assertIn(expected, page)

    def test_cli_split_token_cells_are_visually_deemphasized(self):
        path = self.write([self.row("cursor", 1, True), self.row("pi", 2, True)])
        model = report_page.assemble_tables([{"path": path}], tasks_dirs=[self.tmp.name])[0]
        page = report_page.render_page([model], "Method")
        rows = re.findall(r'<tr style="[^"]+">.*?</tr>', page)
        cursor_row = next(row for row in rows if "cursor × model-x" in row)
        pi_row = next(row for row in rows if "pi × model-x" in row)
        self.assertEqual(cursor_row.count('class="cli-split"'), 3)
        self.assertNotIn('class="cli-split"', pi_row)
        self.assertIn('.cli-split{color:', page)

    def test_cli_marker_uses_underlying_harness_for_named_candidate(self):
        path = self.write([
            self.row("cursor", 1, True,
                     candidate_provenance={"name": "experiment-a", "candidate_digest": "abc"}),
        ])
        model = report_page.assemble_tables([{"path": path}], tasks_dirs=[self.tmp.name])[0]
        self.assertTrue(report_page._chart_data(model)[0]["cli_basis"])

    def test_scatter_gracefully_handles_missing_measurement(self):
        model = {"arms": [{"arm": "cursor", "rate": 1.0,
                           "wilson": [0.2, 1.0], "total_tokens": None,
                           "med_wall": None}]}
        colors = {"cursor": "#0072B2"}
        svg = report_page._scatter_chart(
            report_page._chart_data(model), colors, "total_tokens", "Efficiency", True)
        self.assertIn("Efficiency data unavailable", svg)
        self.assertIn('role="img"', svg)
        self.assertIn("<title>Efficiency by correctness</title>", svg)

    def test_all_charts_remain_accessible_when_metrics_are_missing(self):
        model = {"arms": [{"arm": "cursor", "rate": None, "wilson": None,
                           "total_tokens": None, "med_wall": None}]}
        charts = report_page._charts(model, {"cursor": "#0072B2"})
        self.assertEqual(charts.count('role="img"'), 3)
        self.assertEqual(charts.count("<title>"), 3)
        self.assertIn("data unavailable", charts)

    def test_chart_viewboxes_expand_for_long_harness_labels(self):
        arm = "a-very-long-harness-name-that-must-remain-readable (candidate)"
        data = [{"arm": arm, "rate": .5, "wilson": [.2, .8],
                 "total_tokens": 1000, "med_wall": 10, "cli_basis": False}]
        colors = {arm: "#0072B2"}
        for chart in (report_page._correctness_chart(data, colors),
                      report_page._scatter_chart(data, colors, "total_tokens",
                                                 "Efficiency", True)):
            width = int(re.search(r'viewBox="0 0 (\d+)', chart).group(1))
            self.assertGreater(width, 820)
            self.assertIn(arm, chart)

    def test_duplicate_cells_are_rejected(self):
        path = self.write([self.row("pi", 1, True), self.row("pi", 1, False)])
        with self.assertRaisesRegex(ValueError, "duplicate .* cell"):
            report_page.assemble_tables([{"path": path}], tasks_dirs=[self.tmp.name])

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

    def test_conflicting_matching_policy_for_same_model_is_rejected(self):
        path = self.write([self.row("pi", 1, True)])
        with self.assertRaisesRegex(ValueError, "conflicting matched policies"):
            report_page.assemble_tables([
                {"path": path, "matched": True}, {"path": path, "matched": False}
            ], tasks_dirs=[self.tmp.name])

    def test_candidate_and_baseline_with_same_harness_are_distinct_arms(self):
        path = self.write([
            self.row("codex", 1, True),
            self.row("codex", 1, False,
                     candidate_provenance={"name": "codex", "candidate_digest": "abc"}),
        ])
        model = report_page.assemble_tables([{"path": path}], tasks_dirs=[self.tmp.name])[0]
        self.assertEqual({arm["arm"] for arm in model["arms"]},
                         {"codex", "codex (candidate)"})
        self.assertEqual({arm["n"] for arm in model["arms"]}, {1})

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

    def test_excluded_rows_do_not_trigger_provenance_warning(self):
        path = self.write([
            self.row("pi", 1, True, timeout_s=60),
            self.row("codex", 1, True, timeout_s=60),
            self.row("pi", 2, False, timeout_s=120, failure_class="infra"),
        ])
        model = report_page.assemble_tables([{"path": path}], tasks_dirs=[self.tmp.name])[0]
        self.assertTrue(model["provenance"]["ok"])

    def test_mixed_timeout_provenance_is_prominently_flagged(self):
        path = self.write([
            self.row("pi", 1, True, timeout_s=60),
            self.row("pi", 2, True, timeout_s=120),
            self.row("codex", 1, True, timeout_s=60),
        ])
        models = report_page.assemble_tables([{"path": path}], tasks_dirs=[self.tmp.name])
        page = report_page.render_page(models, "Method")
        self.assertIn("Non-comparable provenance", page)
        self.assertIn("timeout_s mixed within group", page)

    def test_site_build_appends_manifest_and_links_release(self):
        path = self.write([self.row("pi", 1, True)])
        models = report_page.assemble_tables([{"path": path}], tasks_dirs=[self.tmp.name])
        page = report_page.render_page(models, "Method", "Synthetic release")
        site = os.path.join(self.tmp.name, "site")
        destination = report_page.build_site(
            site, "2026-07-20-synthetic", "2026-07-20", "Synthetic release", models, page)
        self.assertTrue(os.path.isfile(destination))
        self.assertEqual(stat.S_IMODE(os.stat(destination).st_mode), 0o644)
        with open(os.path.join(site, "releases.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest[0]["models"], ["model-x"])
        with open(os.path.join(site, "index.html"), encoding="utf-8") as fh:
            index = fh.read()
        self.assertIn('href="releases/2026-07-20-synthetic/index.html"', index)
        self.assertIn("Synthetic release", index)
        self.assertIn("model-x", index)
        report_page.build_site(site, "2026-07-20-synthetic", "2026-07-20",
                               "Synthetic release", models, page)
        with open(os.path.join(site, "releases.json"), encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)), 1)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            report_page.build_site(site, "2026-07-20-synthetic", "2026-07-20",
                                   "Different title", models, page)
        with self.assertRaisesRegex(ValueError, "valid YYYY-MM-DD"):
            report_page.build_site(site, "bad-date", "2026-99-20",
                                   "Synthetic release", models, page)

        with open(os.path.join(site, "releases.json"), "rb") as fh:
            manifest_before = fh.read()
        with open(os.path.join(site, "index.html"), "rb") as fh:
            index_before = fh.read()
        real_replace = os.replace
        calls = 0
        def fail_second_replace(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic publish failure")
            return real_replace(source, destination)
        with mock.patch.object(report_page.os, "replace", side_effect=fail_second_replace):
            with self.assertRaisesRegex(OSError, "synthetic publish failure"):
                report_page.build_site(site, "2026-07-21-synthetic", "2026-07-21",
                                       "Synthetic release 2", models, page)
        self.assertFalse(os.path.exists(os.path.join(
            site, "releases", "2026-07-21-synthetic", "index.html")))
        with open(os.path.join(site, "releases.json"), "rb") as fh:
            self.assertEqual(fh.read(), manifest_before)
        with open(os.path.join(site, "index.html"), "rb") as fh:
            self.assertEqual(fh.read(), index_before)

        os.remove(os.path.join(site, "releases.json"))
        report_page.build_site(site, "2026-07-20-synthetic", "2026-07-20",
                               "Synthetic release", models, page)
        with open(os.path.join(site, "releases.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)[0]["id"], "2026-07-20-synthetic")

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
