#!/usr/bin/env python3
"""Tests for result-row failure classification semantics."""

import os
import tempfile
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import sys
sys.path.insert(0, BENCH_DIR)

import failure_class  # noqa: E402
import run  # noqa: E402


MOONSHOT_429 = (
    "APIError: HTTP 429 rate_limit: TPD rate limit, current 1502271, "
    "limit 1500000. Please retry later."
)


class TestClassifyFailure(unittest.TestCase):
    def test_solved_when_checker_passed(self):
        row = {"success": True, "checker_exit": 0, "error": "timeout after 1s"}
        self.assertEqual(failure_class.classify_failure(row, MOONSHOT_429), "solved")

    def test_moonshot_429_signature_is_rate_limited(self):
        row = {"success": False, "completed": False, "tokens": 0, "turns": 1}
        self.assertEqual(failure_class.classify_failure(row, MOONSHOT_429), "rate_limited")

    def test_rate_limited_beats_timeout_and_wrong_answer(self):
        row = {"success": False, "error": "timeout after 600s", "checker_exit": 1}
        self.assertEqual(failure_class.classify_failure(row, "quota exhausted"), "rate_limited")

    def test_domain_text_about_rate_limiting_is_not_provider_rate_limit(self):
        row = {"success": False, "completed": True, "checker_exit": 1, "error": None}
        transcript = (
            "README says the webcore middleware returns 429 Too Many Requests "
            "when the application's token-bucket rate limit exceeded path is tested."
        )
        self.assertEqual(failure_class.classify_failure(row, transcript), "wrong_answer")

    def test_provider_http_429_context_is_rate_limited(self):
        row = {"success": False, "completed": False, "checker_exit": 1, "error": None}
        self.assertEqual(
            failure_class.classify_failure(row, "HTTP 429 Too Many Requests from API response"),
            "rate_limited",
        )

    def test_infra_markers(self):
        cases = [
            "docker daemon not reachable (is Docker Desktop running?)",
            "container produced no result sentinel (exit 1)",
            "SETUP-NEEDED: export MOONSHOT_API_KEY to use kimi-k2.7-code",
            "missing pi auth at /home/me/.pi/agent/auth.json",
            "No such image: openbench-harness:latest",
        ]
        for text in cases:
            with self.subTest(text=text):
                row = {"success": False, "error": text, "checker_exit": 1}
                self.assertEqual(failure_class.classify_failure(row, ""), "infra")

    def test_infra_beats_timeout(self):
        row = {"success": False, "error": "container timeout; No such image: openbench-harness:latest"}
        self.assertEqual(failure_class.classify_failure(row, ""), "infra")

    def test_timeout_from_error_checker_or_wall_cap(self):
        self.assertEqual(
            failure_class.classify_failure({"success": False, "error": "timeout after 30s"}, ""),
            "timeout",
        )
        self.assertEqual(
            failure_class.classify_failure({"success": False, "checker_exit": "timeout"}, ""),
            "timeout",
        )
        self.assertEqual(
            failure_class.classify_failure({"success": False, "wall_time_s": 599.0}, "", timeout_s=600),
            "timeout",
        )

    def test_wrong_answer_when_agent_finished_and_checker_failed(self):
        row = {"success": False, "completed": True, "checker_exit": 1, "error": None}
        self.assertEqual(failure_class.classify_failure(row, "normal transcript"), "wrong_answer")


class TestRunnerWriteTimeClassification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench_classify_")
        self.tasks_dir = os.path.join(self.tmp, "tasks")
        self.task = "tiny"
        task_dir = os.path.join(self.tasks_dir, self.task)
        os.makedirs(os.path.join(task_dir, "workspace"))
        with open(os.path.join(task_dir, "instruction.md"), "w", encoding="utf-8") as fh:
            fh.write("do it")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_runner_scans_full_output_not_just_tail(self):
        orig_invoke, orig_checker = run.invoke_adapter, run.run_checker

        def fake_invoke(*args, **kwargs):
            return {
                "completed": False,
                "error": "exit 1",
                "output_tail": "tail without marker",
                "full_output": MOONSHOT_429 + "\n" + ("x" * 3000),
                "tokens": 0,
                "turns": 1,
                "cmd": ["fake"],
            }, "local"

        try:
            run.invoke_adapter = fake_invoke
            run.run_checker = lambda *a, **k: (1, None)
            row = run.run_cell(
                "fake", self.task, "kimi-k2.7-code", 1, 600,
                self.tasks_dir, self.tmp, 30,
            )
        finally:
            run.invoke_adapter, run.run_checker = orig_invoke, orig_checker

        self.assertNotIn("output_tail", run.ROW_FIELDS)
        self.assertEqual(row["output_tail"], "tail without marker")  # internal only, not persisted
        self.assertEqual(row["failure_class"], "rate_limited")


if __name__ == "__main__":
    unittest.main()
