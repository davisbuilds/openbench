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
