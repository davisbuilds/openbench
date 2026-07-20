#!/usr/bin/env python3
"""Tests for run.py's in-invocation reliability gates."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock


from obench import run  # noqa: E402


def result_row(failure_class="wrong_answer", tokens=None, error=None):
    return {
        "run_id": "mock-adapter:fixture:model:trial1",
        "success": False,
        "score": 0.0,
        "completed": False,
        "checker_exit": None,
        "exec_mode": "local",
        "failure_class": failure_class,
        "tokens": tokens,
        "error": error,
    }


class RunnerReliabilityGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="run_reliability_")
        self.addCleanup(self.tmp.cleanup)
        self.results_path = os.path.join(self.tmp.name, "results.jsonl")

    def invoke(self, rows, *extra, tasks="a,b,c,d,e"):
        emitted = []
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            "--harness", "mock-adapter",
            "--task", tasks,
            "--results-path", self.results_path,
            *extra,
        ]
        with mock.patch.object(run, "host_version_drift", return_value=[]), \
                mock.patch.object(run, "probe_version", return_value="mock-1"), \
                mock.patch.object(run, "load_existing_run_ids", return_value=set()), \
                mock.patch.object(run, "run_cell", side_effect=rows) as run_cell, \
                mock.patch.object(run, "append_row", side_effect=lambda path, row: emitted.append((path, row))), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = run.main(argv)
        return code, run_cell, emitted, stdout.getvalue(), stderr.getvalue()

    def test_three_instant_infra_cells_abort_remaining_cells_nonzero(self):
        rows = [
            result_row("infra", 0, "expired OAuth"),
            result_row("rate_limited", None, "proxy route missing"),
            result_row("infra", 12, "stale image"),
            result_row("wrong_answer", 500),
        ]
        code, run_cell, emitted, _stdout, stderr = self.invoke(rows)

        self.assertEqual(code, 2)
        self.assertEqual(run_cell.call_count, 3)
        self.assertEqual(len(emitted), 3, "the tripping row must remain written")
        self.assertIn("3 consecutive near-zero-token", stderr)
        self.assertIn("last_error=stale image", stderr)

    def test_wrong_answers_never_trip(self):
        rows = [result_row("wrong_answer", 0) for _ in range(5)]
        code, run_cell, emitted, _stdout, stderr = self.invoke(rows)
        self.assertEqual(code, 0)
        self.assertEqual(run_cell.call_count, 5)
        self.assertEqual(len(emitted), 5)
        self.assertNotIn("circuit breaker", stderr)

    def test_infra_with_real_token_spend_resets_streak(self):
        rows = [
            result_row("infra", 0),
            result_row("infra", None),
            result_row("infra", 100),
            result_row("infra", 0),
            result_row("infra", 99),
        ]
        code, run_cell, _emitted, _stdout, stderr = self.invoke(rows)
        self.assertEqual(code, 0)
        self.assertEqual(run_cell.call_count, 5)
        self.assertNotIn("circuit breaker", stderr)

    def test_capability_failure_resets_infra_streak(self):
        rows = [
            result_row("infra", None),
            result_row("rate_limited", 0),
            result_row("timeout", 0),
            result_row("infra", None),
            result_row("infra", 99),
        ]
        code, run_cell, _emitted, _stdout, stderr = self.invoke(rows)
        self.assertEqual(code, 0)
        self.assertEqual(run_cell.call_count, 5)
        self.assertNotIn("circuit breaker", stderr)

    def test_circuit_breaker_threshold_and_disable_overrides(self):
        infra_rows = [result_row("infra", 0) for _ in range(5)]
        code, run_cell, _emitted, _stdout, _stderr = self.invoke(
            infra_rows, "--max-consecutive-infra", "2")
        self.assertEqual(code, 2)
        self.assertEqual(run_cell.call_count, 2)

        code, run_cell, _emitted, _stdout, _stderr = self.invoke(
            infra_rows, "--max-consecutive-infra", "0")
        self.assertEqual(code, 0)
        self.assertEqual(run_cell.call_count, 5)

    def test_preflight_wrong_answer_passes_and_uses_sidecar(self):
        smoke = result_row("wrong_answer", 25)
        main = result_row("wrong_answer", 500)
        custom_tasks_dir = os.path.join(self.tmp.name, "custom-tasks")
        code, run_cell, emitted, stdout, stderr = self.invoke(
            [smoke, main], "--preflight-smoke", "--tasks-dir", custom_tasks_dir,
            tasks="main-task")

        self.assertEqual(code, 0)
        self.assertEqual(run_cell.call_count, 2)
        smoke_call, main_call = run_cell.call_args_list
        self.assertEqual(smoke_call.args[1:4], ("make-it-run", run.DEFAULT_MODEL, 0))
        self.assertEqual(smoke_call.args[5], run.DEFAULT_TASKS_DIR)
        self.assertEqual(main_call.args[1:4], ("main-task", run.DEFAULT_MODEL, 1))
        self.assertEqual(main_call.args[5], custom_tasks_dir)
        self.assertEqual(emitted[0][0], os.path.join(self.tmp.name, "results.preflight.jsonl"))
        self.assertEqual(emitted[1][0], self.results_path)
        self.assertIn("PREFLIGHT", stdout)
        self.assertEqual(stderr, "")

    def test_preflight_near_zero_infra_refuses_main_run(self):
        smoke = result_row("infra", None, "authentication expired")
        code, run_cell, emitted, _stdout, stderr = self.invoke(
            [smoke], "--preflight-smoke", tasks="main-task")

        self.assertEqual(code, 2)
        self.assertEqual(run_cell.call_count, 1)
        self.assertEqual([path for path, _row in emitted], [
            os.path.join(self.tmp.name, "results.preflight.jsonl")
        ])
        self.assertIn("Refusing to start: preflight smoke", stderr)
        self.assertIn("last_error=authentication expired", stderr)
        self.assertIn("--allow-preflight-failure", stderr)

    def test_preflight_failure_override_runs_main_cells(self):
        smoke = result_row("rate_limited", 0, "HTTP 429")
        main = result_row("wrong_answer", 500)
        code, run_cell, emitted, _stdout, stderr = self.invoke(
            [smoke, main], "--preflight-smoke", "--allow-preflight-failure",
            tasks="main-task")

        self.assertEqual(code, 0)
        self.assertEqual(run_cell.call_count, 2)
        self.assertEqual(len(emitted), 2)
        self.assertIn("WARN: Refusing to start: preflight smoke", stderr)


if __name__ == "__main__":
    unittest.main()
