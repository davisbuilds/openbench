#!/usr/bin/env python3
"""Unit tests for bench/stats.py canonical statistics."""

import json
import os
import sys
import subprocess
import tempfile
import unittest


from obench import stats  # noqa: E402


class StatsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results = os.path.join(self.tmp.name, "results.jsonl")
        self.tasks = os.path.join(self.tmp.name, "tasks")
        os.mkdir(self.tasks)

    def write_rows(self, rows):
        with open(self.results, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def make_task(self, name, dropped=False):
        path = os.path.join(self.tasks, name)
        os.makedirs(path, exist_ok=True)
        if dropped:
            with open(os.path.join(path, "DROPPED.md"), "w", encoding="utf-8") as fh:
                fh.write("quarantined\n")

    def build(self, rows, group="model", min_n=2):
        self.write_rows(rows)
        return stats.build_stats([self.results], group=group, min_n=min_n, tasks_dirs=[self.tasks])


class TestWilsonMath(unittest.TestCase):
    def test_known_values(self):
        lo, hi = stats.wilson_ci(4, 5)
        self.assertAlmostEqual(lo, 0.376, places=2)
        self.assertAlmostEqual(hi, 0.964, places=2)

    def test_n_zero_full_uncertainty(self):
        self.assertEqual(stats.wilson_ci(0, 0), (0.0, 1.0))


class TestExclusionsAndQuarantine(StatsTestCase):
    def test_invalid_rows_removed_from_denominator_and_reported(self):
        self.make_task("t1")
        rows = [
            {"harness": "h", "model": "m", "task": "t1", "trial": 1, "success": True,
             "failure_class": "solved"},
            {"harness": "h", "model": "m", "task": "t1", "trial": 2, "success": "false",
             "failure_class": "wrong_answer"},
            {"harness": "h", "model": "m", "task": "t1", "success": False,
             "failure_class": "wrong_answer"},
        ]
        result = self.build(rows)
        table = result["tables"]["all_countable_non_comparable"]
        self.assertEqual(table[0]["solved"], 1)
        self.assertEqual(table[0]["n"], 1)
        self.assertEqual(result["excluded_counts"], {"invalid_row": 2})

    def test_exclusions_removed_from_denominator_and_reported(self):
        self.make_task("t1")
        rows = [
            {"harness": "h", "model": "m", "task": "t1", "trial": 1, "success": True,
             "failure_class": "solved", "wall_time_s": 10, "tokens_total": 100},
            {"harness": "h", "model": "m", "task": "t1", "trial": 2, "success": False,
             "failure_class": "wrong_answer", "wall_time_s": 20, "tokens_total": 200},
            {"harness": "h", "model": "m", "task": "t1", "trial": 3, "success": False,
             "failure_class": "infra", "wall_time_s": 30, "tokens_total": 300},
            {"harness": "h", "model": "m", "task": "t1", "trial": 4, "success": False,
             "failure_class": "rate_limited", "wall_time_s": 40, "tokens_total": 400},
        ]
        result = self.build(rows)
        table = result["tables"]["all_countable_non_comparable"]
        self.assertEqual(len(table), 1)
        self.assertEqual(table[0]["solved"], 1)
        self.assertEqual(table[0]["n"], 2)
        self.assertEqual(result["excluded_counts"], {"infra": 1, "rate_limited": 1})

    def test_dropped_task_lookup_stays_under_tasks_dir(self):
        outside = os.path.join(self.tmp.name, "outside")
        os.mkdir(outside)
        with open(os.path.join(outside, "DROPPED.md"), "w", encoding="utf-8") as fh:
            fh.write("not a configured task root\n")
        rows = [{"harness": "h", "model": "m", "task": "../outside", "trial": 1,
                 "success": True, "failure_class": "solved"}]
        result = self.build(rows)
        only = result["tables"]["all_countable_non_comparable"][0]
        self.assertEqual(only["n"], 1)
        self.assertEqual(result["excluded_counts"], {})

    def test_dropped_task_quarantined_before_denominator(self):
        self.make_task("active")
        self.make_task("dropped", dropped=True)
        dropped_abs = os.path.join(self.tasks, "dropped")
        rows = [
            {"harness": "h", "model": "m", "task": "active", "trial": 1, "success": True,
             "failure_class": "solved"},
            {"harness": "h", "model": "m", "task": "dropped", "trial": 1, "success": True,
             "failure_class": "solved"},
            {"harness": "h", "model": "m", "task": dropped_abs, "trial": 2, "success": False,
             "failure_class": "wrong_answer"},
        ]
        result = self.build(rows)
        only = result["tables"]["all_countable_non_comparable"][0]
        self.assertEqual(only["n"], 1)
        self.assertEqual(only["solved"], 1)
        self.assertEqual(result["excluded_counts"], {"quarantined_dropped_task": 2})
        self.assertEqual(result["quarantined_tasks"], {"dropped": 1, dropped_abs: 1})


class TestMatchedDenominators(StatsTestCase):
    def test_duplicate_matched_cells_are_reported_not_overwritten(self):
        self.make_task("t1")
        rows = [
            {"harness": "h", "model": "a", "task": "t1", "trial": 1, "success": True,
             "failure_class": "solved"},
            {"harness": "h", "model": "a", "task": "t1", "trial": 1, "success": False,
             "failure_class": "wrong_answer"},
            {"harness": "h", "model": "b", "task": "t1", "trial": 1, "success": True,
             "failure_class": "solved"},
        ]
        result = self.build(rows, group="model", min_n=1)
        all_rows = {row["group"]: row for row in result["tables"]["all_countable_non_comparable"]}
        self.assertEqual(all_rows["a"]["n"], 2)
        self.assertEqual(result["matched"]["matched_cells_per_group"], 0)
        self.assertEqual(result["matched"]["duplicate_cells_excluded"], 1)
        self.assertEqual(result["matched"]["duplicate_rows_excluded"], 2)

    def test_model_group_matches_task_harness_trial_cells(self):
        for task in ("t1", "t2"):
            self.make_task(task)
        rows = [
            # Common cells for both models: (t1, h, trial1), (t2, h, trial1).
            {"harness": "h", "model": "a", "task": "t1", "trial": 1, "success": True,
             "failure_class": "solved"},
            {"harness": "h", "model": "b", "task": "t1", "trial": 1, "success": False,
             "failure_class": "wrong_answer"},
            {"harness": "h", "model": "a", "task": "t2", "trial": 1, "success": False,
             "failure_class": "wrong_answer"},
            {"harness": "h", "model": "b", "task": "t2", "trial": 1, "success": True,
             "failure_class": "solved"},
            # Unmatched extra cell only for model a; appears in all-countable only.
            {"harness": "h", "model": "a", "task": "t1", "trial": 2, "success": True,
             "failure_class": "solved"},
        ]
        result = self.build(rows, group="model", min_n=1)
        all_rows = {row["group"]: row for row in result["tables"]["all_countable_non_comparable"]}
        matched = {row["group"]: row for row in result["tables"]["matched_comparable"]}
        self.assertEqual(all_rows["a"]["solved"], 2)
        self.assertEqual(all_rows["a"]["n"], 3)
        self.assertEqual(matched["a"]["solved"], 1)
        self.assertEqual(matched["a"]["n"], 2)
        self.assertEqual(matched["b"]["solved"], 1)
        self.assertEqual(matched["b"]["n"], 2)
        self.assertEqual(result["matched"]["matched_cells_per_group"], 2)
        self.assertEqual(result["matched"]["unmatched_countable_rows"], 1)


class TestFlagsEfficiencyAndCost(StatsTestCase):
    def test_low_n_and_solved_only_medians(self):
        self.make_task("t1")
        rows = [
            {"harness": "h", "model": "m", "task": "t1", "trial": 1, "success": True,
             "failure_class": "solved", "wall_time_s": 10, "t_agent_s": 8, "tokens_total": 100,
             "tokens_input": 80, "tokens_output": 20, "score": 1.0},
            {"harness": "h", "model": "m", "task": "t1", "trial": 2, "success": True,
             "failure_class": "solved", "wall_time_s": 30, "t_agent_s": 12, "tokens_total": 300,
             "tokens_input": 200, "tokens_output": 100, "score": 1.0},
            {"harness": "h", "model": "m", "task": "t1", "trial": 3, "success": False,
             "failure_class": "wrong_answer", "wall_time_s": 999, "tokens_total": 999,
             "tokens_input": 999, "tokens_output": 999, "score": 0.25},
        ]
        result = self.build(rows, min_n=5)
        row = result["tables"]["all_countable_non_comparable"][0]
        self.assertTrue(row["low_n"])
        self.assertEqual(row["flags"], ["LOW-N"])
        self.assertEqual(row["median_wall_time_s_solved"], 20)
        self.assertEqual(row["median_t_agent_s_solved"], 10)
        self.assertEqual(row["median_tokens_total_solved"], 200)
        self.assertEqual(row["median_tokens_input_solved"], 140)
        self.assertEqual(row["median_tokens_output_solved"], 60)
        self.assertAlmostEqual(row["mean_score"], 0.75)

    def test_non_finite_numbers_and_out_of_range_scores_are_ignored(self):
        self.make_task("t1")
        rows = [
            {"harness": "h", "model": "m", "task": "t1", "trial": 1, "success": True,
             "failure_class": "solved", "wall_time_s": float("inf"), "tokens_total": float("nan"),
             "tokens_input": -1, "tokens_output": 20, "score": float("nan")},
            {"harness": "h", "model": "m", "task": "t1", "trial": 2, "success": False,
             "failure_class": "wrong_answer", "score": 100.0},
        ]
        result = self.build(rows, min_n=1)
        row = result["tables"]["all_countable_non_comparable"][0]
        self.assertEqual(row["mean_score"], 0.5)
        self.assertIsNone(row["median_wall_time_s_solved"])
        self.assertIsNone(row["median_tokens_total_solved"])
        self.assertIsNone(row["median_tokens_input_solved"])
        self.assertEqual(row["median_tokens_output_solved"], 20)

    def test_pricing_adds_cost_per_solve(self):
        self.make_task("t1")
        rows = [
            {"harness": "h", "model": "m", "task": "t1", "trial": 1, "success": True,
             "failure_class": "solved", "tokens_input": 1_000_000, "tokens_output": 500_000},
            {"harness": "h", "model": "m", "task": "t1", "trial": 2, "success": False,
             "failure_class": "wrong_answer", "tokens_input": 9_000_000, "tokens_output": 9_000_000},
        ]
        self.write_rows(rows)
        pricing = {"m": {"input_per_mtok": 2.0, "output_per_mtok": 10.0}}
        result = stats.build_stats([self.results], group="model", tasks_dirs=[self.tasks], pricing=pricing)
        row = result["tables"]["all_countable_non_comparable"][0]
        self.assertEqual(row["median_cost_solved"], 7.0)


class TestProvenanceGate(StatsTestCase):
    def provenance_rows(self, digest_a="sha256:same", digest_b="sha256:same"):
        self.make_task("t1")
        return [
            {"harness": "h", "model": "a", "task": "t1", "trial": 1, "success": True,
             "failure_class": "solved", "image_digest": digest_a, "harness_version": "hv1",
             "timeout_s": 1200, "checker_digest": "check1"},
            {"harness": "h", "model": "b", "task": "t1", "trial": 1, "success": False,
             "failure_class": "wrong_answer", "image_digest": digest_b, "harness_version": "hv1",
             "timeout_s": 1200, "checker_digest": "check1"},
        ]

    def test_same_digest_is_provenance_ok(self):
        result = self.build(self.provenance_rows(), group="model", min_n=1)
        self.assertTrue(result["provenance_ok"])
        self.assertEqual(result["provenance"]["shared"]["image_digest"], ["sha256:same"])
        text = stats.render_text(result)
        self.assertIn("PROVENANCE: OK (all compared groups share", text)
        self.assertIn("image_digest=sha256:same", text)

    def test_differing_digests_are_flagged(self):
        result = self.build(self.provenance_rows(digest_b="sha256:other"), group="model", min_n=1)
        self.assertFalse(result["provenance_ok"])
        flags = result["provenance"]["flags"]
        self.assertTrue(any(flag["field"] == "image_digest" and
                            flag["type"] == "differs_across_groups" for flag in flags))
        text = stats.render_text(result)
        self.assertIn("NON-COMPARABLE: image_digest differs across groups", text)
        self.assertIn("a: ['sha256:same']", text)
        self.assertGreaterEqual(text.count("NON-COMPARABLE"), 2)

    def test_harness_version_compared_per_harness_not_per_group(self):
        # Different harnesses at different versions inside one model group is
        # normal, NOT a provenance flag.
        self.make_task("t1")
        rows = []
        for model in ("a", "b"):
            for harness, hv in (("pi", "0.80.6"), ("codex", "0.144.0")):
                rows.append({"harness": harness, "model": model, "task": "t1", "trial": 1,
                             "success": True, "failure_class": "solved",
                             "image_digest": "sha256:same", "harness_version": hv,
                             "timeout_s": 1200})
        result = self.build(rows, group="model", min_n=1)
        self.assertTrue(result["provenance_ok"])

    def test_same_harness_version_drift_across_groups_is_flagged(self):
        self.make_task("t1")
        rows = [
            {"harness": "pi", "model": "a", "task": "t1", "trial": 1, "success": True,
             "failure_class": "solved", "image_digest": "sha256:same",
             "harness_version": "0.80.3", "timeout_s": 1200},
            {"harness": "pi", "model": "b", "task": "t1", "trial": 1, "success": True,
             "failure_class": "solved", "image_digest": "sha256:same",
             "harness_version": "0.80.6", "timeout_s": 1200},
        ]
        result = self.build(rows, group="model", min_n=1)
        self.assertFalse(result["provenance_ok"])
        flags = result["provenance"]["flags"]
        self.assertTrue(any(flag["field"] == "harness_version" and
                            flag["type"] == "differs_across_groups" for flag in flags))

    def test_mixed_within_group_is_flagged(self):
        self.make_task("t1")
        self.make_task("t2")
        rows = [
            {"harness": "h", "model": "a", "task": "t1", "trial": 1, "success": True,
             "failure_class": "solved", "image_digest": "sha256:one", "harness_version": "hv1"},
            {"harness": "h", "model": "a", "task": "t2", "trial": 1, "success": True,
             "failure_class": "solved", "image_digest": "sha256:two", "harness_version": "hv1"},
            {"harness": "h", "model": "b", "task": "t1", "trial": 1, "success": False,
             "failure_class": "wrong_answer", "image_digest": "sha256:one", "harness_version": "hv1"},
            {"harness": "h", "model": "b", "task": "t2", "trial": 1, "success": False,
             "failure_class": "wrong_answer", "image_digest": "sha256:one", "harness_version": "hv1"},
        ]
        result = self.build(rows, group="model", min_n=1)
        self.assertFalse(result["provenance_ok"])
        self.assertTrue(any(flag["field"] == "image_digest" and
                            flag["type"] == "mixed_within_group" and
                            flag["group"] == "a" for flag in result["provenance"]["flags"]))
        self.assertIn("image_digest has mixed values within group a", stats.render_text(result))

    def test_absent_image_digest_is_info_not_flag(self):
        self.make_task("t1")
        rows = [
            {"harness": "h", "model": "a", "task": "t1", "trial": 1, "success": True,
             "failure_class": "solved"},
            {"harness": "h", "model": "b", "task": "t1", "trial": 1, "success": False,
             "failure_class": "wrong_answer"},
        ]
        result = self.build(rows, group="model", min_n=1)
        self.assertTrue(result["provenance_ok"])
        self.assertEqual(result["provenance"]["unknown_provenance_rows"], 2)
        self.assertIn("unknown provenance: 2 rows", stats.render_text(result))

    def test_partially_absent_image_digest_is_info_not_flag(self):
        self.make_task("t1")
        rows = [
            {"harness": "h", "model": "a", "task": "t1", "trial": 1, "success": True,
             "failure_class": "solved", "image_digest": "sha256:same", "harness_version": "hv1"},
            {"harness": "h", "model": "b", "task": "t1", "trial": 1, "success": False,
             "failure_class": "wrong_answer", "harness_version": "hv1"},
        ]
        result = self.build(rows, group="model", min_n=1)
        self.assertTrue(result["provenance_ok"])
        self.assertEqual(result["provenance"]["unknown_provenance_rows"], 1)
        self.assertEqual(result["provenance"]["fields"]["image_digest"]["missing_by_group"]["b"], 1)
        self.assertIn("unknown provenance: 1 rows", stats.render_text(result))

    def test_non_finite_optional_provenance_is_ignored_not_crash(self):
        self.make_task("t1")
        rows = [
            {"harness": "h", "model": "a", "task": "t1", "trial": 1, "success": True,
             "failure_class": "solved", "image_digest": "sha256:same", "harness_version": "hv1",
             "timeout_s": float("nan")},
            {"harness": "h", "model": "b", "task": "t1", "trial": 1, "success": False,
             "failure_class": "wrong_answer", "image_digest": "sha256:same", "harness_version": "hv1",
             "timeout_s": 1200},
        ]
        result = self.build(rows, group="model", min_n=1)
        self.assertTrue(result["provenance_ok"])
        self.assertEqual(result["provenance"]["fields"]["timeout_s"]["missing_by_group"]["a"], 1)
        text = stats.render_text(result)
        self.assertIn("PROVENANCE: OK", text)
        self.assertIn("missing timeout_s: 1 rows", text)

    def test_strict_provenance_exits_2_on_flag(self):
        self.write_rows(self.provenance_rows(digest_b="sha256:other"))
        cmd = [sys.executable, "-m", "obench.stats",
               self.results, "--group", "model", "--tasks-dir", self.tasks, "--strict-provenance"]
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("NON-COMPARABLE: image_digest differs across groups", proc.stdout)


class TestCliOutput(StatsTestCase):
    def test_cli_text_and_json(self):
        self.make_task("t1")
        self.write_rows([{"harness": "h", "model": "m", "task": "t1", "trial": 1,
                          "success": True, "failure_class": "solved"}])
        cmd = [sys.executable, "-m", "obench.stats",
               self.results, "--group", "model", "--tasks-dir", self.tasks]
        text = subprocess.check_output(cmd, text=True)
        self.assertIn("ALL COUNTABLE ROWS (NON-COMPARABLE", text)
        raw = subprocess.check_output(cmd + ["--json"], text=True)
        payload = json.loads(raw)
        self.assertTrue(payload["provenance_ok"])
        self.assertEqual(payload["tables"]["all_countable_non_comparable"][0]["group"], "m")


if __name__ == "__main__":
    unittest.main()
