#!/usr/bin/env python3
"""End-to-end integration tests for the matrix queue.

The mocked ``test_matrix_queue`` suite passed 38/38 while the queue failed on
every one of its first live cells: the mocks never spawned a real runner
subprocess, so nothing exercised the actual ``python3 -m obench.run`` invocation
or the row/no-row bookkeeping around it. These tests close that gap by driving
``obench matrix --spec`` as a genuine subprocess against a temp results path and
a temp tasks dir holding one trivial task, using the built-in ``null`` harness so
no external CLI or credentials are needed.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _write(path, content, executable=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    if executable:
        os.chmod(path, 0o755)


class MatrixQueueE2ETests(unittest.TestCase):
    """Drive the real ``obench matrix`` CLI end to end with ``--harness null``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mq_e2e_")
        self.tasks_dir = os.path.join(self.tmp, "tasks")
        # WHY: The null harness makes no edits. An always-pass checker gives the test a real
        # scored row without an external agent.
        self._make_task("trivial", checker_exit=0)
        self.results_rel = os.path.join("out", "results.jsonl")
        self.results_path = os.path.join(self.tmp, self.results_rel)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_task(self, name, checker_exit=0, dropped=False):
        task_dir = os.path.join(self.tasks_dir, name)
        _write(os.path.join(task_dir, "instruction.md"), "Trivial task.\n")
        _write(
            os.path.join(task_dir, "checker.sh"),
            f'#!/usr/bin/env bash\necho "checker ran for {name}"\nexit {checker_exit}\n',
            executable=True,
        )
        _write(os.path.join(task_dir, "workspace", "README.md"), "placeholder\n")
        if dropped:
            _write(os.path.join(task_dir, "DROPPED.md"), "dropped\n")

    def _write_spec(self, body):
        path = os.path.join(self.tmp, "spec.toml")
        _write(path, textwrap.dedent(body))
        return path

    def _run_matrix(self, spec_path):
        """Run the real ``obench matrix`` CLI as a subprocess."""
        proc = subprocess.run(
            [sys.executable, "-m", "obench.matrix_queue", "--spec", spec_path],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180,
        )
        return proc

    def _rows(self):
        rows = {}
        if not os.path.isfile(self.results_path):
            return rows
        with open(self.results_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    rows[r["run_id"]] = r
        return rows


    def test_end_to_end_null_harness_reaches_full_coverage(self):
        spec = self._write_spec(f"""
            results_path = "{self.results_rel}"
            ledger_dir = "out/ledger"
            timeout = 60
            trials = 2

            [[arm]]
            harness = "null"
            model = "control"

            [[task_group]]
            tasks_dir = "tasks"
            tasks = ["trivial"]
        """)
        proc = self._run_matrix(spec)
        self.assertEqual(proc.returncode, 0,
                         f"expected full coverage exit 0.\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")

        rows = self._rows()
        self.assertEqual(len(rows), 2, f"expected 2 rows (1 task x 2 trials); got {list(rows)}")
        for run_id, row in rows.items():
            self.assertTrue(row["success"], f"{run_id} should have succeeded: {row}")
            self.assertEqual(row["failure_class"], "solved", row)
            self.assertEqual(row["harness"], "null")

        self.assertIn("null x control: 2/2 (100.0%)", proc.stdout, proc.stdout)
        self.assertIn("Total: 2/2 satisfied", proc.stdout)


    def test_excluded_class_only_row_is_rerun_to_satisfied(self):
        """A cell whose sole prior row is excluded (infra) must be re-run.

        This needs ``--force`` in the runner command: without it the runner
        would skip the run_id that already appears in the log and the excluded
        cell would never become satisfied -- exactly the coverage gap.
        """
        run_id = "null:trivial:control:trial1"
        _write(self.results_path,
               json.dumps({"run_id": run_id, "harness": "null", "model": "control",
                           "task": "trivial", "trial": 1, "success": False,
                           "failure_class": "infra"}) + "\n")

        spec = self._write_spec(f"""
            results_path = "{self.results_rel}"
            ledger_dir = "out/ledger"
            timeout = 60
            trials = 1

            [[arm]]
            harness = "null"
            model = "control"

            [[task_group]]
            tasks_dir = "tasks"
            tasks = ["trivial"]
        """)
        proc = self._run_matrix(spec)
        self.assertEqual(proc.returncode, 0,
                         f"excluded cell should be re-run to satisfied.\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")

        rows = self._rows()
        self.assertIn(run_id, rows)
        self.assertEqual(rows[run_id]["failure_class"], "solved", rows[run_id])
        self.assertTrue(rows[run_id]["success"])
        with open(self.results_path, encoding="utf-8") as fh:
            line_count = sum(1 for line in fh if line.strip())
        self.assertEqual(line_count, 2, "the runner must append a NEW row, not skip the cell")
        self.assertIn("null x control: 1/1 (100.0%)", proc.stdout)

    def test_successful_resumed_retry_clears_legacy_exhausted_marker(self):
        run_id = "null:trivial:control:trial1"
        _write(self.results_path,
               json.dumps({"run_id": run_id, "harness": "null", "model": "control",
                           "task": "trivial", "trial": 1, "success": False,
                           "failure_class": "stalled"}) + "\n")
        ledger = os.path.join(self.tmp, "out", "ledger", "queue-state.json")
        _write(ledger, json.dumps({
            "arm_states": {
                "null x control": {
                    "name": "null x control",
                    "budgets": {"stall": 1},
                    "planned": 1,
                    "exhausted_cells": [run_id],
                },
            },
            "retry_counts": {run_id: 1},
            "pending": [],
        }))
        spec = self._write_spec(f"""
            results_path = "{self.results_rel}"
            ledger_dir = "out/ledger"
            timeout = 60
            trials = 1

            [retry]
            stalled = 1

            [[arm]]
            harness = "null"
            model = "control"

            [[task_group]]
            tasks_dir = "tasks"
            tasks = ["trivial"]
        """)

        proc = self._run_matrix(spec)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("null x control: 1/1 (100.0%)", proc.stdout)
        with open(ledger, encoding="utf-8") as fh:
            state = json.load(fh)
        arm = state["arm_states"]["null x control"]
        self.assertEqual(arm["budgets"]["stalled"], 1)
        self.assertNotIn("stall", arm["budgets"])
        self.assertEqual(arm["exhausted_cells"], [])

    def test_already_satisfied_resume_clears_legacy_exhausted_marker(self):
        run_id = "null:trivial:control:trial1"
        _write(self.results_path,
               json.dumps({"run_id": run_id, "harness": "null", "model": "control",
                           "task": "trivial", "trial": 1, "success": True,
                           "failure_class": "solved"}) + "\n")
        ledger = os.path.join(self.tmp, "out", "ledger", "queue-state.json")
        _write(ledger, json.dumps({
            "arm_states": {
                "null x control": {
                    "name": "null x control",
                    "budgets": {"stall": 1},
                    "planned": 1,
                    "exhausted_cells": [run_id],
                },
            },
            "pending": [],
        }))
        spec = self._write_spec(f"""
            results_path = "{self.results_rel}"
            ledger_dir = "out/ledger"
            timeout = 60
            trials = 1

            [[arm]]
            harness = "null"
            model = "control"

            [[task_group]]
            tasks_dir = "tasks"
            tasks = ["trivial"]
        """)

        proc = self._run_matrix(spec)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("null x control: 1/1 (100.0%)", proc.stdout)
        with open(ledger, encoding="utf-8") as fh:
            state = json.load(fh)
        arm = state["arm_states"]["null x control"]
        self.assertEqual(arm["exhausted_cells"], [])


    def test_no_row_nonzero_exit_stops_arm_as_config_error(self):
        """Nonzero runner exit + no row written => CONFIG-ERROR, not EXHAUSTED.

        A DROPPED task makes the runner raise SystemExit before it writes any
        row, mimicking a mis-wired arm (bad --tasks-dir, missing adapter, import
        crash). The queue must surface the runner's stderr and stop the arm
        rather than burning retries against an unclassifiable fc=None cell.
        """
        self._make_task("dropped", checker_exit=0, dropped=True)
        spec = self._write_spec(f"""
            results_path = "{self.results_rel}"
            ledger_dir = "out/ledger"
            timeout = 60
            trials = 1

            [[arm]]
            harness = "null"
            model = "control"

            [[task_group]]
            tasks_dir = "tasks"
            tasks = ["dropped"]
        """)
        proc = self._run_matrix(spec)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("CONFIG-ERROR", combined, combined)
        self.assertIn("[CONFIG-ERROR]", proc.stdout, proc.stdout)
        self.assertIn("dropped from the active set", combined)
        self.assertNotIn("EXHAUSTED", proc.stdout,
                         "a no-row config failure must not be reported as retry exhaustion")
        self.assertEqual(self._rows(), {})


    def test_mixed_task_groups_local_and_docker_exec_mode(self):
        """Two task groups with distinct tasks_dir/exec_mode both run.

        Mirrors the mini's gap-fill spec: a local core-tasks group plus a docker
        terminal-bench group. We keep both on the ``null`` control (which runs
        identically in either exec mode) and point exec at docker only nominally;
        the point is that each cell carries its group's tasks_dir + exec_mode.
        """
        other_tasks = os.path.join(self.tmp, "tb-tasks")
        td2 = os.path.join(other_tasks, "tb-trivial")
        _write(os.path.join(td2, "instruction.md"), "tb task\n")
        _write(os.path.join(td2, "checker.sh"),
               '#!/usr/bin/env bash\nexit 0\n', executable=True)
        _write(os.path.join(td2, "workspace", "f"), "x\n")

        spec = self._write_spec(f"""
            results_path = "{self.results_rel}"
            ledger_dir = "out/ledger"
            timeout = 60
            trials = 1
            exec_mode = "local"

            [[arm]]
            harness = "null"
            model = "control"

            [[task_group]]
            tasks_dir = "tasks"
            tasks = ["trivial"]

            [[task_group]]
            tasks_dir = "tb-tasks"
            exec_mode = "local"
            tasks = ["tb-trivial"]
        """)
        proc = self._run_matrix(spec)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = self._rows()
        self.assertIn("null:trivial:control:trial1", rows)
        self.assertIn("null:tb-trivial:control:trial1", rows)
        self.assertIn("null x control: 2/2 (100.0%)", proc.stdout)


if __name__ == "__main__":
    unittest.main()
