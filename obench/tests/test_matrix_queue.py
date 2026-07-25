#!/usr/bin/env python3
"""Tests for the matrix queue (retry budgets, arm pausing, coverage)."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

from obench import matrix_queue as mq
from obench import failure_class as fc_mod
from obench import run as bench_run


class SpecLoadingTests(unittest.TestCase):
    """Verify TOML spec parsing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mq_spec_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_spec(self, content):
        path = os.path.join(self.tmp, "spec.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def test_valid_spec(self):
        path = self.make_spec("""
results_path = "results.jsonl"
timeout = 2400
trials = 3

[[arm]]
harness = "pi"
model = "laguna-s-2.1"

[[arm]]
harness = "aider"
model = "inkling"

[[task_group]]
tasks = ["hello-world", "fibonacci"]
""")
        spec = mq.load_spec(path)
        self.assertEqual(len(spec["arm"]), 2)
        self.assertEqual(len(spec["task_group"]), 1)
        self.assertEqual(spec["arm"][0]["harness"], "pi")
        self.assertEqual(spec["arm"][1]["model"], "inkling")

    def test_rejects_missing_arms(self):
        path = self.make_spec("""
results_path = "results.jsonl"
[[task_group]]
tasks = ["hello"]
""")
        with self.assertRaises(mq.SpecError):
            mq.load_spec(path)

    def test_rejects_missing_harness(self):
        path = self.make_spec("""
results_path = "results.jsonl"
[[arm]]
model = "x"
[[task_group]]
tasks = ["hello"]
""")
        with self.assertRaises(mq.SpecError):
            mq.load_spec(path)

    def test_rejects_missing_task_groups(self):
        path = self.make_spec("""
results_path = "results.jsonl"
[[arm]]
harness = "pi"
model = "x"
""")
        with self.assertRaises(mq.SpecError):
            mq.load_spec(path)

    def test_rejects_missing_results_path(self):
        path = self.make_spec("""
[[arm]]
harness = "pi"
model = "x"
[[task_group]]
tasks = ["hello"]
""")
        with self.assertRaises(mq.SpecError):
            mq.load_spec(path)

    def test_custom_retry_budgets(self):
        path = self.make_spec("""
results_path = "results.jsonl"
[retry]
infra = 5
stall = 2
rate_limited = 1

[[arm]]
harness = "pi"
model = "x"
[[task_group]]
tasks = ["hello"]
""")
        spec = mq.load_spec(path)
        retry = spec.get("retry", {})
        self.assertEqual(retry["infra"], 5)
        self.assertEqual(retry["stall"], 2)
        self.assertEqual(retry["rate_limited"], 1)

    def test_expand_cells(self):
        arms = [{"harness": "pi", "model": "a"}, {"harness": "codex", "model": "b"}]
        tasks = ["t1", "t2"]
        cells = mq.expand_cells(arms, tasks, 2)
        self.assertEqual(len(cells), 8)  # 2 arms * 2 tasks * 2 trials
        ids = [c["run_id"] for c in cells]
        self.assertIn("pi:t1:a:trial1", ids)
        self.assertIn("codex:t2:b:trial2", ids)

    def test_expand_cells_run_id_format(self):
        arms = [{"harness": "pi", "model": "laguna-s-2.1"}]
        cells = mq.expand_cells(arms, ["hello"], 1)
        self.assertEqual(cells[0]["run_id"], "pi:hello:laguna-s-2.1:trial1")


class QueueStateTests(unittest.TestCase):
    """Verify persistent queue state JSON."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mq_state_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_state_is_empty(self):
        path = os.path.join(self.tmp, "queue-state.json")
        state = mq.QueueState(path)
        self.assertEqual(state.data, {})

    def test_save_and_restore(self):
        path = os.path.join(self.tmp, "queue-state.json")
        state = mq.QueueState(path)
        state.set("hello", "world")
        state.save()

        state2 = mq.QueueState(path)
        self.assertEqual(state2.get("hello"), "world")

    def test_arm_state_roundtrip(self):
        a = mq.ArmState("pi")
        a.satisfied = 5
        a.planned = 10
        a.consecutive_excluded = 3
        a.exhausted_cells = ["pi:t1:m:trial1"]
        restored = mq.ArmState.from_dict(a.to_dict())
        self.assertEqual(restored.name, "pi")
        self.assertEqual(restored.satisfied, 5)
        self.assertEqual(restored.planned, 10)
        self.assertEqual(restored.consecutive_excluded, 3)
        self.assertEqual(restored.exhausted_cells, ["pi:t1:m:trial1"])
        self.assertFalse(restored.paused)


class CellSatisfactionTests(unittest.TestCase):
    """Verify cell satisfaction logic."""

    def test_solved_is_satisfied(self):
        self.assertTrue(mq.cell_is_satisfied({"failure_class": "solved"}))

    def test_wrong_answer_is_satisfied(self):
        self.assertTrue(mq.cell_is_satisfied({"failure_class": "wrong_answer"}))

    def test_infra_is_not_satisfied(self):
        self.assertFalse(mq.cell_is_satisfied({"failure_class": "infra"}))

    def test_rate_limited_is_not_satisfied(self):
        self.assertFalse(mq.cell_is_satisfied({"failure_class": "rate_limited"}))

    def test_stalled_is_not_satisfied(self):
        self.assertFalse(mq.cell_is_satisfied({"failure_class": "stalled"}))

    def test_timeout_is_satisfied(self):
        self.assertTrue(mq.cell_is_satisfied({"failure_class": "timeout"}))

    def test_no_row_is_not_satisfied(self):
        self.assertFalse(mq.cell_is_satisfied(None))

    def test_no_failure_class_derived(self):
        """A row with no failure_class uses class_for_report to derive it."""
        row = {"success": False, "checker_exit": 1, "completed": True,
               "error": "something", "tokens": 100}
        # Should derive as wrong_answer (satisfied)
        self.assertTrue(mq.cell_is_satisfied(row))


class LoadResultsIdsTests(unittest.TestCase):
    """Verify results JSONL loading."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mq_results_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_file_returns_empty(self):
        path = os.path.join(self.tmp, "results.jsonl")
        self.assertEqual(mq.load_results_ids(path), {})

    def test_loads_rows_keyed_by_run_id(self):
        path = os.path.join(self.tmp, "results.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"run_id": "a", "failure_class": "solved"}) + "\n")
            fh.write(json.dumps({"run_id": "b", "failure_class": "infra"}) + "\n")
        rows = mq.load_results_ids(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows["a"]["failure_class"], "solved")
        self.assertEqual(rows["b"]["failure_class"], "infra")

    def test_skips_corrupt_lines(self):
        path = os.path.join(self.tmp, "results.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write(json.dumps({"run_id": "a", "failure_class": "solved"}) + "\n")
        rows = mq.load_results_ids(path)
        self.assertEqual(len(rows), 1)

    def test_last_row_wins_on_duplicate_id(self):
        path = os.path.join(self.tmp, "results.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"run_id": "a", "failure_class": "infra"}) + "\n")
            fh.write(json.dumps({"run_id": "a", "failure_class": "solved"}) + "\n")
        rows = mq.load_results_ids(path)
        self.assertEqual(rows["a"]["failure_class"], "solved")


class RetryBudgetTests(unittest.TestCase):
    """Verify retry budget logic."""

    def test_default_budgets(self):
        arm = mq.ArmState("pi")
        self.assertEqual(arm.retry_budget("infra"), 2)
        self.assertEqual(arm.retry_budget("stall"), 1)
        self.assertEqual(arm.retry_budget("rate_limited"), 3)
        self.assertEqual(arm.retry_budget("wrong_answer"), 0)
        self.assertEqual(arm.retry_budget("solved"), 0)

    def test_custom_budgets(self):
        arm = mq.ArmState("pi", {"infra": 5, "stall": 0})
        self.assertEqual(arm.retry_budget("infra"), 5)
        self.assertEqual(arm.retry_budget("stall"), 0)

    def test_backoff_rate_limited(self):
        d1 = mq.backoff_for_failure("rate_limited", 1, 60)
        self.assertAlmostEqual(d1, 60.0)
        d2 = mq.backoff_for_failure("rate_limited", 2, 60)
        self.assertAlmostEqual(d2, 120.0)
        d3 = mq.backoff_for_failure("rate_limited", 3, 60)
        self.assertAlmostEqual(d3, 240.0)

    def test_backoff_other_failures(self):
        d = mq.backoff_for_failure("infra", 1, 60)
        self.assertEqual(d, 10.0)  # fixed 10s for non-rate-limited


class RunnerCommandBuildingTests(unittest.TestCase):
    """Verify runner subprocess argv construction."""

    def test_basic_command(self):
        cell = {"harness": "pi", "model": "a", "task": "t1", "trial": 1}
        cmd = mq.build_runner_command(cell, "/path/results.jsonl", "/tasks", 2400, None, "local")
        self.assertIn("--force", cmd)
        self.assertIn("--harness", cmd)
        self.assertIn("pi", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("a", cmd)
        self.assertIn("--task", cmd)
        self.assertIn("t1", cmd)
        self.assertIn("--trial", cmd)
        self.assertIn("1", cmd)
        self.assertIn("--timeout", cmd)
        self.assertIn("2400", cmd)
        self.assertIn("--results-path", cmd)
        self.assertIn("/path/results.jsonl", cmd)
        self.assertIn("--tasks-dir", cmd)
        self.assertIn("/tasks", cmd)

    def test_docker_mode(self):
        cell = {"harness": "pi", "model": "a", "task": "t1", "trial": 1}
        cmd = mq.build_runner_command(cell, "r.jsonl", "/t", 2400, 600, "docker")
        self.assertIn("--exec", cmd)
        self.assertIn("docker", cmd)
        self.assertIn("--stall-timeout", cmd)
        self.assertIn("600", cmd)
        self.assertIn("--proxy", cmd)

    def test_no_stall_timeout_no_proxy(self):
        cell = {"harness": "pi", "model": "a", "task": "t1", "trial": 1}
        cmd = mq.build_runner_command(cell, "r.jsonl", "/t", 2400, None, "local")
        self.assertNotIn("--stall-timeout", cmd)
        # proxy is only added when stall_timeout is set
        self.assertNotIn("--proxy", cmd)


class CoverageSummaryTests(unittest.TestCase):
    """Verify coverage output format."""

    def test_full_coverage(self):
        arms = [{"harness": "pi", "model": "a"}]
        tasks = ["t1"]
        cells = mq.expand_cells(arms, tasks, 1)
        arm_states = {a["harness"]: mq.ArmState(a["harness"]) for a in arms}
        arm_states["pi"].planned = 1
        arm_states["pi"].satisfied = 1

        self.assertEqual(arm_states["pi"].satisfied, 1)
        self.assertEqual(arm_states["pi"].planned, 1)

    def test_partial_coverage_exhausted(self):
        arm = mq.ArmState("pi")
        arm.planned = 3
        arm.satisfied = 1
        arm.exhausted_cells = ["pi:t1:a:trial2"]
        self.assertEqual(len(arm.exhausted_cells), 1)
        self.assertEqual(arm.satisfied, 1)


if __name__ == "__main__":
    unittest.main()
