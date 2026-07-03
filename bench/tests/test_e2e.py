#!/usr/bin/env python3
"""End-to-end test driving run.py through the full pipeline via subprocess.

Uses a tiny fixture task (``write-marker``) plus a fixture adapter
(``fake_adapter``) that actually solves it. Verifies:
  - the null adapter yields success=false (negative control),
  - the fake adapter yields success=true (writes the marker file),
  - resumability: a second invocation skips cells whose run_id already exists,
  - report.build_report aggregates the produced results without error.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(BENCH_DIR, "tests", "fixtures")
RUN_PY = os.path.join(BENCH_DIR, "run.py")

sys.path.insert(0, BENCH_DIR)

import report  # noqa: E402


def read_rows(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def rows_by_id(rows):
    return {row["run_id"]: row for row in rows}


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench_e2e_")
        self.results_path = os.path.join(self.tmp, "results.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, harness):
        proc = subprocess.run(
            [sys.executable, RUN_PY,
             "--task", "write-marker",
             "--harness", harness,
             "--model", "gpt-5.5-medium",
             "--results-path", self.results_path,
             "--adapters-dir", FIXTURES_DIR,
             "--tasks-dir", FIXTURES_DIR],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0,
                         msg=f"run.py failed:\n{proc.stdout}\n{proc.stderr}")
        return proc

    def test_relative_tasks_dir_from_other_cwd(self):
        # Regression: the checker runs with cwd=temp workdir, so a relative
        # --tasks-dir must still resolve. Drive run.py from the repo root with
        # relative dirs and confirm the fake adapter still solves the task.
        rel_fixtures = os.path.relpath(FIXTURES_DIR, BENCH_DIR)  # tests/fixtures
        rel_fixtures = os.path.join("bench", rel_fixtures)
        results = os.path.join(self.tmp, "rel.jsonl")
        proc = subprocess.run(
            [sys.executable, "bench/run.py",
             "--task", "write-marker",
             "--harness", "fake_adapter",
             "--results-path", results,
             "--adapters-dir", rel_fixtures,
             "--tasks-dir", rel_fixtures],
            cwd=os.path.dirname(BENCH_DIR),  # repo root
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        rows = read_rows(results)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["success"],
                        "relative --tasks-dir must still let the checker run")
        self.assertEqual(rows[0]["checker_exit"], 0)

    def test_null_adapter_fails_and_fake_adapter_succeeds(self):
        self._run("null")
        self._run("fake_adapter")

        rows = read_rows(self.results_path)
        by_id = rows_by_id(rows)

        null_id = "null:write-marker:gpt-5.5-medium:trial1"
        fake_id = "fake_adapter:write-marker:gpt-5.5-medium:trial1"
        self.assertIn(null_id, by_id)
        self.assertIn(fake_id, by_id)

        null_row = by_id[null_id]
        self.assertTrue(null_row["completed"], "null adapter should complete")
        self.assertFalse(null_row["success"], "null adapter must not solve task")
        self.assertNotEqual(null_row["checker_exit"], 0)

        fake_row = by_id[fake_id]
        self.assertTrue(fake_row["completed"])
        self.assertTrue(fake_row["success"], "fake adapter should solve task")
        self.assertEqual(fake_row["checker_exit"], 0)
        self.assertEqual(fake_row["tokens"], 42)
        self.assertEqual(fake_row["turns"], 1)
        self.assertIsInstance(fake_row["wall_time_s"], (int, float))

    def test_resumability_skips_existing_cells(self):
        self._run("fake_adapter")
        first = read_rows(self.results_path)
        self.assertEqual(len(first), 1)

        proc = self._run("fake_adapter")
        second = read_rows(self.results_path)
        # No new row appended; skip reported.
        self.assertEqual(len(second), 1, "resumed run must not duplicate the cell")
        self.assertIn("SKIP", proc.stdout)
        self.assertIn("skipped=1", proc.stdout)

    def test_force_reruns_cell(self):
        self._run("fake_adapter")
        proc = subprocess.run(
            [sys.executable, RUN_PY,
             "--task", "write-marker",
             "--harness", "fake_adapter",
             "--model", "gpt-5.5-medium",
             "--results-path", self.results_path,
             "--adapters-dir", FIXTURES_DIR,
             "--tasks-dir", FIXTURES_DIR,
             "--force"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        rows = read_rows(self.results_path)
        self.assertEqual(len(rows), 2, "--force should append a second row")
        self.assertIn("ran=1", proc.stdout)

    def test_report_aggregates_results(self):
        self._run("null")
        self._run("fake_adapter")
        text = report.build_report(self.results_path)
        self.assertIn("null", text)
        self.assertIn("fake_adapter", text)
        self.assertIn("write-marker", text)
        self.assertIn("wilson95", text)

    def test_efficiency_report_reflects_fixture_tokens(self):
        # The fixture adapter reports tokens=42, turns=1 and solves the task, so
        # the efficiency view must surface tokens/turns per solve for it and a
        # dash for the null control (which reports neither).
        self._run("null")
        self._run("fake_adapter")
        eff = report.build_efficiency_report(self.results_path)
        self.assertIn("turns/slv", eff)
        fake = next(l for l in eff.splitlines() if l.startswith("fake_adapter"))
        self.assertIn("42", fake)   # tokens/solve = 42/1
        self.assertIn("1.0", fake)  # turns/solve  = 1/1
        null = next(l for l in eff.splitlines() if l.startswith("null"))
        self.assertTrue(null.rstrip().endswith("-"))  # null: no turn data

    def test_checker_timeout(self):
        # The slow-checker fixture sleeps 30s; a 1s --checker-timeout must abort
        # it and record checker_exit="timeout", success=false, without hanging.
        proc = subprocess.run(
            [sys.executable, RUN_PY,
             "--task", "slow-checker",
             "--harness", "null",
             "--model", "gpt-5.5-medium",
             "--checker-timeout", "1",
             "--results-path", self.results_path,
             "--adapters-dir", FIXTURES_DIR,
             "--tasks-dir", FIXTURES_DIR],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        rows = read_rows(self.results_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["checker_exit"], "timeout")
        self.assertFalse(row["success"], "a timed-out checker must not succeed")

    def test_pristine_workspace_untouched(self):
        # Solving the task writes done.txt into a temp copy, never the fixture.
        marker = os.path.join(FIXTURES_DIR, "write-marker", "workspace", "done.txt")
        self._run("fake_adapter")
        self.assertFalse(os.path.exists(marker),
                         "runner must not write into the pristine workspace")


if __name__ == "__main__":
    unittest.main()
