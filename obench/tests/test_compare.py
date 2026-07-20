#!/usr/bin/env python3
"""Unit and CLI tests for the matched comparison scorecard."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import compare  # noqa: E402


class CompareTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks = os.path.join(self.tmp.name, "tasks")
        os.mkdir(self.tasks)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def write(self, name, rows):
        path = self.path(name)
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        return path

    @staticmethod
    def row(harness, task, trial, success, **extra):
        row = {
            "harness": harness,
            "model": "m",
            "task": task,
            "trial": trial,
            "success": success,
            "failure_class": "solved" if success else "wrong_answer",
            "score": 1.0 if success else 0.0,
            "wall_time_s": 10,
            "tokens_input_uncached": 100,
            "tokens_cache_read": 20,
            "tokens_output": 30,
            "harness_version": "1.0",
            "timeout_s": 2400,
        }
        row.update(extra)
        return row


class TestMatchedComparison(CompareTestCase):
    def test_path_labels_cannot_collide_with_generated_suffixes(self):
        labels = compare._path_labels(["a.jsonl", "a.jsonl", "a-2.jsonl"])
        self.assertEqual(len(labels), len(set(labels)))

    def test_multiple_files_use_intersection_and_per_solve_totals(self):
        a = self.write("alpha.jsonl", [
            self.row("h", "t1", 1, True, wall_time_s=10, tokens_input_uncached=100),
            self.row("h", "t2", 1, False, wall_time_s=20, tokens_input_uncached=200,
                     score=0.5),
            self.row("h", "only-a", 1, True),
        ])
        b = self.write("beta.jsonl", [
            self.row("h", "t1", 1, False),
            self.row("h", "t2", 1, True),
        ])

        result = compare.build_comparison([a, b], tasks_dirs=[self.tasks])
        self.assertEqual(result["matched_n"], 2)
        alpha = result["summaries"]["alpha"]
        self.assertEqual((alpha["solved"], alpha["n"]), (1, 2))
        self.assertEqual(alpha["hack_adjusted_rate"], 0.75)
        self.assertEqual(alpha["wall_time_per_solve"], 30)
        self.assertEqual(alpha["tokens_input_uncached_per_solve"], 300)
        self.assertEqual(alpha["unmatched_countable_rows"], 1)

    def test_proxy_token_columns_render_when_canonical_tokens_are_null(self):
        proxy_tokens = {
            "tokens_input_uncached": None,
            "tokens_cache_read": None,
            "tokens_cache_write": None,
            "tokens_output": None,
            "token_basis": None,
            "tokens_proxy_input_uncached": 111,
            "tokens_proxy_cache_read": 22,
            "tokens_proxy_cache_write": 3,
            "tokens_proxy_output": 44,
            "token_basis_proxy": "proxy_measured",
        }
        a = self.write("pi.jsonl", [
            self.row("pi", "t", 1, True, **proxy_tokens),
        ])
        b = self.write("opencode.jsonl", [
            self.row("opencode", "t", 1, True, **proxy_tokens),
        ])

        report = compare.build_comparison([a, b], tasks_dirs=[self.tasks])
        rendered_rows = dict(compare.scorecard_rows(report))
        self.assertEqual(rendered_rows["Uncached input tokens / solve"], ["111.0", "111.0"])
        self.assertEqual(rendered_rows["Cache-read tokens / solve"], ["22.0", "22.0"])
        self.assertEqual(rendered_rows["Cache-write tokens / solve"], ["3.0", "3.0"])
        self.assertEqual(rendered_rows["Output tokens / solve"], ["44.0", "44.0"])

    def test_solved_intersection_uses_only_cells_every_arm_solved_for_efficiency(self):
        a = self.write("a.jsonl", [
            self.row("h", "only-a-solve", 1, True, wall_time_s=100),
            self.row("h", "only-b-solve", 1, False, wall_time_s=200),
            self.row("h", "both-1", 1, True, wall_time_s=10,
                     tokens_input_uncached=10),
            self.row("h", "both-2", 1, True, wall_time_s=20,
                     tokens_input_uncached=20),
            self.row("h", "both-3", 1, True, wall_time_s=60,
                     tokens_input_uncached=60),
        ])
        b = self.write("b.jsonl", [
            self.row("h", "only-a-solve", 1, False, wall_time_s=300),
            self.row("h", "only-b-solve", 1, True, wall_time_s=400),
            self.row("h", "both-1", 1, True, wall_time_s=40,
                     tokens_input_uncached=40),
            self.row("h", "both-2", 1, True, wall_time_s=50,
                     tokens_input_uncached=50),
            self.row("h", "both-3", 1, True, wall_time_s=120,
                     tokens_input_uncached=120),
        ])

        report = compare.build_comparison(
            [a, b], tasks_dirs=[self.tasks], solved_intersection=True)
        self.assertEqual(report["matched_n"], 5)
        self.assertEqual(report["all_solved_n"], 3)
        self.assertEqual(report["summaries"]["a"]["solved"], 4)
        self.assertEqual(report["summaries"]["b"]["solved"], 4)
        self.assertEqual(report["summaries"]["a"]["wall_time_per_cell_mean"], 30)
        self.assertEqual(report["summaries"]["a"]["wall_time_per_cell_median"], 20)
        self.assertEqual(report["summaries"]["b"]["tokens_input_uncached_per_cell_mean"], 70)
        self.assertEqual(report["summaries"]["b"]["tokens_input_uncached_per_cell_median"], 50)
        self.assertIn("All-solved n: 3 of 5 matched", compare.render_text(report))

    def test_empty_solved_intersection_has_clear_message_and_unavailable_metrics(self):
        a = self.write("a.jsonl", [self.row("h", "t", 1, True)])
        b = self.write("b.jsonl", [self.row("h", "t", 1, False)])
        report = compare.build_comparison(
            [a, b], tasks_dirs=[self.tasks], solved_intersection=True)

        self.assertEqual(report["all_solved_n"], 0)
        message = ("All-solved n: 0 of 1 matched "
                   "(no efficiency cells; efficiency metrics unavailable)")
        self.assertIn(message, compare.render_text(report))
        self.assertIn(f"**{message}**", compare.render_markdown(report))
        rows = dict(compare.scorecard_rows(report))
        self.assertEqual(rows["Wall time / cell mean (s)"], ["-", "-"])
        self.assertEqual(rows["Output tokens / cell median"], ["-", "-"])

    def test_solved_intersection_markdown_snapshot(self):
        a = self.write("a.jsonl", [
            self.row("h", "both", 1, True, wall_time_s=10,
                     tokens_cache_write=5),
            self.row("h", "split", 1, True, wall_time_s=99,
                     tokens_cache_write=99),
        ])
        b = self.write("b.jsonl", [
            self.row("h", "both", 1, True, wall_time_s=20,
                     tokens_cache_write=7),
            self.row("h", "split", 1, False, wall_time_s=88,
                     tokens_cache_write=88),
        ])
        report = compare.build_comparison(
            [a, b], tasks_dirs=[self.tasks], solved_intersection=True)
        fixture = os.path.join(os.path.dirname(__file__), "fixtures",
                               "compare_solved_intersection.md")
        with open(fixture, encoding="utf-8") as fh:
            expected = fh.read()
        self.assertEqual(compare.render_markdown(report), expected)

    def test_failure_exclusions_are_per_arm_and_remove_cell_from_intersection(self):
        dropped = os.path.join(self.tasks, "dropped")
        os.mkdir(dropped)
        with open(os.path.join(dropped, "DROPPED.md"), "w", encoding="utf-8") as fh:
            fh.write("quarantined\n")
        a = self.write("a.jsonl", [
            self.row("h", "common", 1, True),
            self.row("h", "infra", 1, False, failure_class="infra"),
            self.row("h", "limited", 1, False, failure_class="rate_limited"),
            self.row("h", "dropped", 1, False, failure_class="infra"),
        ])
        b = self.write("b.jsonl", [
            self.row("h", "common", 1, False),
            self.row("h", "infra", 1, True),
            self.row("h", "limited", 1, True),
        ])

        result = compare.build_comparison([a, b], tasks_dirs=[self.tasks])
        self.assertEqual(result["matched_n"], 1)
        self.assertEqual(result["summaries"]["a"]["excluded"],
                         {"infra": 1, "quarantined_dropped_task": 1,
                          "rate_limited": 1})
        self.assertEqual(result["summaries"]["a"]["n"], 1)
        self.assertEqual(result["summaries"]["b"]["unmatched_countable_rows"], 2)

    def test_single_file_splits_harness_and_candidate_arms(self):
        candidate = {"name": "codex-on", "candidate_digest": "short"}
        rows = [
            self.row("codex", "t1", 1, True),
            self.row("codex", "t1", 1, False, candidate_provenance=candidate),
        ]
        path = self.write("combined.jsonl", rows)
        result = compare.build_comparison([path], tasks_dirs=[self.tasks])
        self.assertEqual(result["arms"], ["codex", "codex-on"])
        self.assertEqual(result["matched_n"], 1)

    def test_candidate_name_cannot_collapse_into_baseline_harness(self):
        candidate = {"name": "codex", "candidate_digest": "short"}
        path = self.write("combined.jsonl", [
            self.row("codex", "t1", 1, True),
            self.row("codex (candidate)", "t1", 1, True),
            self.row("codex", "t1", 1, False, candidate_provenance=candidate),
        ])
        result = compare.build_comparison([path], tasks_dirs=[self.tasks])
        self.assertEqual(result["arms"], ["codex", "codex (candidate)",
                                          "codex (candidate)-2"])
        self.assertEqual(result["matched_n"], 1)

    def test_timeout_caps_mixed_within_or_across_arms_are_non_comparable(self):
        a = self.write("a.jsonl", [
            self.row("h", "t1", 1, True, timeout_s=1200),
            self.row("h", "t2", 1, False, timeout_s=2400),
        ])
        b = self.write("b.jsonl", [
            self.row("h", "t1", 1, True, timeout_s=2400),
            self.row("h", "t2", 1, True, timeout_s=2400),
        ])

        report = compare.build_comparison([a, b], tasks_dirs=[self.tasks])
        self.assertFalse(report["provenance_ok"])
        rendered = compare.render_text(report)
        self.assertIn("NON-COMPARABLE PROVENANCE", rendered)
        self.assertIn("timeout_s has mixed values within group a", rendered)
        self.assertIn("1200, 2400 [MIXED]", rendered)

        across = self.write("across.jsonl", [
            self.row("h", "t1", 1, True, timeout_s=600),
            self.row("h", "t2", 1, True, timeout_s=600),
        ])
        report = compare.build_comparison([b, across], tasks_dirs=[self.tasks])
        self.assertFalse(report["provenance_ok"])
        self.assertIn("timeout_s differs across groups", compare.render_text(report))

    def test_missing_timeout_is_unknown_and_warned_once_but_not_fatal(self):
        a = self.write("a.jsonl", [self.row("h", "t", 1, True, timeout_s=None)])
        b = self.write("b.jsonl", [self.row("h", "t", 1, True)])
        report = compare.build_comparison([a, b], tasks_dirs=[self.tasks])

        self.assertTrue(report["provenance_ok"])
        rendered = compare.render_text(report)
        self.assertEqual(rendered.count("timeout_s unknown"), 1)
        self.assertEqual(dict(compare.scorecard_rows(report))["Timeout cap (s)"],
                         ["unknown", "2400"])

    def test_finished_solve_rate_excludes_timeouts_with_its_own_wilson_ci(self):
        a = self.write("a.jsonl", [
            self.row("h", "solved", 1, True),
            self.row("h", "wrong", 1, False),
            self.row("h", "timed", 1, False, failure_class="timeout"),
        ])
        b = self.write("b.jsonl", [
            self.row("h", "solved", 1, True),
            self.row("h", "wrong", 1, False),
            self.row("h", "timed", 1, True),
        ])
        report = compare.build_comparison([a, b], tasks_dirs=[self.tasks])
        a_summary = report["summaries"]["a"]
        b_summary = report["summaries"]["b"]

        self.assertEqual((a_summary["solved"], a_summary["n"]), (1, 3))
        self.assertEqual((a_summary["finished_solved"], a_summary["finished_n"]), (1, 2))
        self.assertEqual(a_summary["finished_solve_rate"], 0.5)
        self.assertEqual((b_summary["finished_solved"], b_summary["finished_n"]), (2, 3))
        rows = dict(compare.scorecard_rows(report))
        self.assertEqual(rows["Solve rate"], ["33.3%", "66.7%"])
        self.assertEqual(rows["Solve rate @cap"], ["33.3%", "66.7%"])
        self.assertEqual(rows["Wilson 95% CI @cap"], rows["Wilson 95% CI"])
        self.assertEqual(rows["Solve rate (finished)"], ["50.0%", "66.7%"])
        self.assertEqual(rows["Excluded: timeout"], ["1", "0"])
        self.assertNotEqual(rows["Wilson 95% CI"], rows["Wilson 95% CI (finished)"])
        self.assertIn("Solve rate (finished)", compare.render_text(report))
        markdown = compare.render_markdown(report)
        self.assertIn("| Solve rate (finished) | 50.0% | 66.7% |", markdown)

    def test_version_mix_is_flagged_in_human_and_markdown_tables(self):
        a = self.write("a.jsonl", [
            self.row("h", "t1", 1, True, harness_version="1.0"),
            self.row("h", "t2", 1, True, harness_version="1.1"),
            self.row("h", "only-a", 1, True, harness_version="1.2"),
        ])
        b = self.write("b.jsonl", [
            self.row("h", "t1", 1, False, harness_version="2.0"),
            self.row("h", "t2", 1, True, harness_version="2.0"),
        ])
        result = compare.build_comparison([a, b], tasks_dirs=[self.tasks])
        self.assertTrue(result["summaries"]["a"]["version_mixed"])
        self.assertFalse(result["provenance_ok"])
        self.assertIn("NON-COMPARABLE PROVENANCE", compare.render_text(result))
        self.assertIn("1.0, 1.1, 1.2 [MIXED]", compare.render_text(result))
        markdown = compare.render_markdown(result)
        self.assertIn("**Matched n: 2**", markdown)
        self.assertIn("| Harness version | 1.0, 1.1, 1.2 [MIXED] | 2.0 |", markdown)

    def test_markdown_escapes_input_derived_table_content(self):
        a = self.write("a|arm.jsonl", [
            self.row("h", "t", 1, True, harness_version="one|two\\three\nfour<img>"),
        ])
        b = self.write("b.jsonl", [self.row("h", "t", 1, True)])
        markdown = compare.render_markdown(
            compare.build_comparison([a, b], tasks_dirs=[self.tasks]))
        self.assertIn("a\\|arm", markdown)
        self.assertIn("one\\|two\\\\three<br>four&lt;img&gt;", markdown)

    def test_invalid_json_does_not_become_a_single_file_arm(self):
        path = self.write("combined.jsonl", [
            self.row("a", "t", 1, True), self.row("b", "t", 1, True),
        ])
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("not-json\n")
        result = compare.build_comparison([path], tasks_dirs=[self.tasks])
        self.assertEqual(result["arms"], ["a", "b"])
        self.assertEqual(result["matched_n"], 1)
        self.assertEqual(result["unassigned_excluded"], {"invalid_json": 1})
        self.assertIn("Unassigned exclusions: invalid_json=1", compare.render_text(result))

    def test_duplicate_rows_are_counted_as_unmatched(self):
        a = self.write("a.jsonl", [
            self.row("h", "t", 1, True), self.row("h", "dup", 1, True),
            self.row("h", "dup", 1, False),
        ])
        b = self.write("b.jsonl", [self.row("h", "t", 1, True)])
        result = compare.build_comparison([a, b], tasks_dirs=[self.tasks])
        self.assertEqual(result["summaries"]["a"]["unmatched_countable_rows"], 2)
        self.assertEqual(result["summaries"]["a"]["duplicate_cells_excluded"], 1)

    def test_warns_on_three_near_zero_wrong_answers(self):
        a = self.write("a.jsonl", [
            self.row("h", f"t{i}", 1, False, tokens_input_uncached=None,
                     tokens_cache_read=None, tokens_output=0, wall_time_s=10 + i * 10)
            for i in range(3)
        ])
        b = self.write("b.jsonl", [
            self.row("h", f"t{i}", 1, True) for i in range(3)
        ])
        report = compare.build_comparison([a, b], tasks_dirs=[self.tasks])
        self.assertTrue(any("near-zero agent tokens" in item
                            for item in report["anomalies"]))
        self.assertIn("ANOMALY [a]", compare.render_text(report))
        self.assertIn("> ANOMALY [a]", compare.render_markdown(report))

    def test_warns_on_three_uniform_failure_wall_times(self):
        a = self.write("a.jsonl", [
            self.row("h", f"t{i}", 1, False, wall_time_s=wall,
                     tokens_input_uncached=1000)
            for i, wall in enumerate((342.0, 343.0, 344.0))
        ])
        b = self.write("b.jsonl", [
            self.row("h", f"t{i}", 1, True) for i in range(3)
        ])
        report = compare.build_comparison([a, b], tasks_dirs=[self.tasks])
        self.assertTrue(any("uniform wall times" in item
                            for item in report["anomalies"]))

    def test_no_anomaly_below_tripwire_threshold(self):
        a = self.write("a.jsonl", [
            self.row("h", f"t{i}", 1, False, tokens_output=0,
                     tokens_input_uncached=None, wall_time_s=100 + i * 100)
            for i in range(2)
        ])
        b = self.write("b.jsonl", [self.row("h", f"t{i}", 1, True)
                                    for i in range(2)])
        report = compare.build_comparison([a, b], tasks_dirs=[self.tasks])
        self.assertEqual(report["anomalies"], [])

    def test_no_shared_cells_renders_zero_denominator(self):
        a = self.write("a.jsonl", [self.row("h", "a", 1, True)])
        b = self.write("b.jsonl", [self.row("h", "b", 1, True)])
        result = compare.build_comparison([a, b], tasks_dirs=[self.tasks])
        self.assertEqual(result["matched_n"], 0)
        self.assertIn("0/0", compare.render_text(result))


class TestCompareCli(CompareTestCase):
    def test_cli_prints_table_and_writes_markdown(self):
        a = self.write("a.jsonl", [self.row("h", "t", 1, True)])
        b = self.write("b.jsonl", [self.row("h", "t", 1, False)])
        markdown = self.path("report/scorecard.md")
        script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "compare.py")
        proc = subprocess.run(
            [sys.executable, script, a, b, "--tasks-dir", self.tasks,
             "--markdown", markdown],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Matched n: 1", proc.stdout)
        self.assertIn("Wilson 95% CI", proc.stdout)
        with open(markdown, encoding="utf-8") as fh:
            scorecard = fh.read()
        self.assertIn("# OpenBench comparison scorecard", scorecard)
        self.assertIn("| Solve rate | 100.0% | 0.0% |", scorecard)

    def test_cli_solved_intersection_prints_and_writes_efficiency_denominator(self):
        a = self.write("a.jsonl", [self.row("h", "t", 1, True)])
        b = self.write("b.jsonl", [self.row("h", "t", 1, True)])
        markdown = self.path("scorecard.md")
        script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "compare.py")
        proc = subprocess.run(
            [sys.executable, script, a, b, "--tasks-dir", self.tasks,
             "--solved-intersection", "--markdown", markdown],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("All-solved n: 1 of 1 matched", proc.stdout)
        self.assertIn("Wall time / cell median (s)", proc.stdout)
        with open(markdown, encoding="utf-8") as fh:
            self.assertIn("**All-solved n: 1 of 1 matched**", fh.read())

    def test_strict_provenance_exits_two_on_version_drift(self):
        a = self.write("a.jsonl", [self.row("h", "t", 1, True, harness_version="1")])
        b = self.write("b.jsonl", [self.row("h", "t", 1, True, harness_version="2")])
        script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "compare.py")
        proc = subprocess.run(
            [sys.executable, script, a, b, "--tasks-dir", self.tasks,
             "--strict-provenance"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("NON-COMPARABLE PROVENANCE", proc.stdout)


if __name__ == "__main__":
    unittest.main()
