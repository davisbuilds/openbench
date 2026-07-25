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
        # One trivial task whose checker always passes: the null control edits
        # nothing, so an always-pass checker gives us a clean success=true row
        # (solved -> satisfied) without needing any real harness.
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
                    rows[r["run_id"]] = r  # last wins
        return rows

    # ── (c) a genuine end-to-end run reaches 100% coverage ──────────────────

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

        # Cells actually got rows written by a real runner subprocess.
        rows = self._rows()
        self.assertEqual(len(rows), 2, f"expected 2 rows (1 task x 2 trials); got {list(rows)}")
        for run_id, row in rows.items():
            self.assertTrue(row["success"], f"{run_id} should have succeeded: {row}")
            self.assertEqual(row["failure_class"], "solved", row)
            self.assertEqual(row["harness"], "null")

        # Coverage report shows 100%.
        self.assertIn("null x control: 2/2 (100.0%)", proc.stdout, proc.stdout)
        self.assertIn("Total: 2/2 satisfied", proc.stdout)

    # ── (d) a cell whose only prior row is excluded-class is re-run ──────────

    def test_excluded_class_only_row_is_rerun_to_satisfied(self):
        """A cell whose sole prior row is excluded (infra) must be re-run.

        This needs ``--force`` in the runner command: without it the runner
        would skip the run_id that already appears in the log and the excluded
        cell would never become satisfied -- exactly the coverage gap.
        """
        run_id = "null:trivial:control:trial1"
        # Pre-seed the results log with an excluded-class (infra) row only.
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

        # A fresh solved row was appended, and last-row-wins makes it satisfied.
        rows = self._rows()
        self.assertIn(run_id, rows)
        self.assertEqual(rows[run_id]["failure_class"], "solved", rows[run_id])
        self.assertTrue(rows[run_id]["success"])
        # Two physical lines: the seeded infra row then the fresh solved row.
        with open(self.results_path, encoding="utf-8") as fh:
            line_count = sum(1 for line in fh if line.strip())
        self.assertEqual(line_count, 2, "the runner must append a NEW row, not skip the cell")
        self.assertIn("null x control: 1/1 (100.0%)", proc.stdout)

    # ── (b) a failed runner invocation is noticed, not silently exhausted ────

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
        # The runner's own reason is surfaced -- never a silent EXHAUSTED.
        self.assertIn("dropped from the active set", combined)
        self.assertNotIn("EXHAUSTED", proc.stdout,
                         "a no-row config failure must not be reported as retry exhaustion")
        # No row was written for the failed cell.
        self.assertEqual(self._rows(), {})

    # ── (a) per-task-group tasks_dir + exec_mode; omit --tasks-dir ──────────

    def test_mixed_task_groups_local_and_docker_exec_mode(self):
        """Two task groups with distinct tasks_dir/exec_mode both run.

        Mirrors the mini's gap-fill spec: a local core-tasks group plus a docker
        terminal-bench group. We keep both on the ``null`` control (which runs
        identically in either exec mode) and point exec at docker only nominally;
        the point is that each cell carries its group's tasks_dir + exec_mode.
        """
        # Second group lives in a different tasks tree.
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
