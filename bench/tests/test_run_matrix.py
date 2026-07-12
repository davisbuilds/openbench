#!/usr/bin/env python3
"""Tests for bench/run_matrix.py."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BENCH_DIR)

import run_matrix  # noqa: E402


class RunMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench_run_matrix_")
        self.tasks_dir = os.path.join(self.tmp, "tasks")
        os.makedirs(self.tasks_dir)
        self.out = os.path.join(self.tmp, "results.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_task(self, name, dropped=False):
        path = os.path.join(self.tasks_dir, name)
        os.makedirs(path)
        with open(os.path.join(path, "instruction.md"), "w", encoding="utf-8") as fh:
            fh.write("fixture task\n")
        if dropped:
            with open(os.path.join(path, "DROPPED.md"), "w", encoding="utf-8") as fh:
                fh.write("dropped\n")
        return path

    def write_row(self, run_id):
        with open(self.out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"run_id": run_id}) + "\n")

    def argv(self, *extra):
        return [
            "--harness", "pi,codex",
            "--model", "a,b",
            "--task", "t1,t2",
            "--trials", "3",
            "--out", self.out,
            "--tasks-dir", self.tasks_dir,
            "--skip-gate",
            *extra,
        ]

    def test_dry_run_expands_cross_product(self):
        self.make_task("t1")
        self.make_task("t2")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = run_matrix.main(self.argv("--dry-run"))
        self.assertEqual(code, 0)
        lines = stdout.getvalue().splitlines()
        cells = [line for line in lines if line.startswith("RUN ")]
        self.assertEqual(len(cells), 24)
        self.assertIn("RUN  pi:t1:a:trial1", cells)
        self.assertIn("RUN  codex:t2:b:trial3", cells)
        self.assertIn("dry-run: cells=24 runnable=24 skipped=0", lines[-1])

    def test_dedup_skip_uses_existing_run_ids(self):
        self.make_task("t1")
        self.make_task("t2")
        self.write_row("pi:t1:a:trial1")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = run_matrix.main(self.argv("--dry-run"))
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn("SKIP pi:t1:a:trial1", text)
        self.assertIn("dry-run: cells=24 runnable=23 skipped=1", text)

    def test_refuses_unknown_task(self):
        self.make_task("t1")
        # t2 never created: wrong --tasks-dir / typo must be caught up front
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = run_matrix.main(self.argv("--dry-run"))
        self.assertEqual(code, 1)
        self.assertIn("unknown task 't2'", stderr.getvalue())

    def test_refuses_dropped_task(self):
        self.make_task("t1", dropped=True)
        self.make_task("t2")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = run_matrix.main(self.argv("--dry-run"))
        self.assertEqual(code, 1)
        self.assertIn("refusing dropped task 't1'", stderr.getvalue())

    def test_sequential_runner_invocation_can_be_stubbed(self):
        self.make_task("t1")
        calls = []

        def fake_run(cmd):
            calls.append(cmd)
            run_id = None
            harness = model = task = None
            trial = None
            for i, part in enumerate(cmd):
                if part == "--harness":
                    harness = cmd[i + 1]
                elif part == "--model":
                    model = cmd[i + 1]
                elif part == "--task":
                    task = cmd[i + 1]
                elif part == "--trial":
                    trial = int(cmd[i + 1])
            run_id = f"{harness}:{task}:{model}:trial{trial}"
            self.write_row(run_id)
            return 0

        argv = [
            "--harness", "pi",
            "--model", "a",
            "--task", "t1",
            "--trials", "2",
            "--out", self.out,
            "--tasks-dir", self.tasks_dir,
            "--skip-gate",
            "--runner-cmd", f"{sys.executable} stub-run.py",
        ]
        stdout = io.StringIO()
        with mock.patch.object(run_matrix, "run_cell_subprocess", side_effect=fake_run), \
                contextlib.redirect_stdout(stdout):
            code = run_matrix.main(argv)

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("CELL pi:t1:a:trial1", stdout.getvalue())
        self.assertIn("CELL pi:t1:a:trial2", stdout.getvalue())
        self.assertIn("Done. ran=2 skipped=0", stdout.getvalue())
        self.assertEqual(calls[0][:2], [sys.executable, "stub-run.py"])
        self.assertIn("--results-path", calls[0])
        self.assertIn("--tasks-dir", calls[0])
        self.assertIn("--trial", calls[0])
        self.assertNotIn("--trials", calls[0])

    def test_runner_success_without_expected_row_is_error(self):
        self.make_task("t1")
        argv = [
            "--harness", "pi",
            "--model", "a",
            "--task", "t1",
            "--trials", "1",
            "--out", self.out,
            "--tasks-dir", self.tasks_dir,
            "--skip-gate",
            "--runner-cmd", f"{sys.executable} stub-run.py",
        ]
        stderr = io.StringIO()
        with mock.patch.object(run_matrix, "run_cell_subprocess", return_value=0), \
                contextlib.redirect_stderr(stderr):
            code = run_matrix.main(argv)

        self.assertEqual(code, 1)
        self.assertIn("did not append pi:t1:a:trial1", stderr.getvalue())

    def test_ctrl_c_between_cells_stops_before_next_launch(self):
        self.make_task("t1")
        calls = []

        def fake_run(cmd):
            calls.append(cmd)
            self.write_row(f"pi:t1:a:trial{len(calls)}")
            run_matrix._STOP_AFTER_CURRENT = True
            return 0

        argv = [
            "--harness", "pi",
            "--model", "a",
            "--task", "t1",
            "--trials", "2",
            "--out", self.out,
            "--tasks-dir", self.tasks_dir,
            "--skip-gate",
            "--runner-cmd", f"{sys.executable} stub-run.py",
        ]
        stderr = io.StringIO()
        stdout = io.StringIO()
        with mock.patch.object(run_matrix, "run_cell_subprocess", side_effect=fake_run), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = run_matrix.main(argv)

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("Stopped after current cell due to Ctrl-C", stderr.getvalue())

    def test_missing_gate_record_warns_not_blocks(self):
        self.make_task("t1")
        stderr = io.StringIO()
        stdout = io.StringIO()
        argv = [
            "--harness", "pi",
            "--model", "a",
            "--task", "t1",
            "--trials", "1",
            "--out", self.out,
            "--tasks-dir", self.tasks_dir,
            "--dry-run",
        ]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = run_matrix.main(argv)
        self.assertEqual(code, 0)
        self.assertIn("WARN: no admission gate PASS record", stderr.getvalue())
        self.assertIn("RUN  pi:t1:a:trial1", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
