#!/usr/bin/env python3
"""Tests for pre-admission task gates."""

import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BENCH_DIR)

import admission_gate  # noqa: E402
import determinism_check  # noqa: E402


class AdmissionGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="admission_gate_tests_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_task(self, name, checker, workspace=None, solution=None, checker_data=None, provenance=True):
        task = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(task, "workspace"))
        os.makedirs(os.path.join(task, "solution"))
        os.makedirs(os.path.join(task, "checker_data"))
        with open(os.path.join(task, "instruction.md"), "w", encoding="utf-8") as fh:
            fh.write("Do the fixture task.\n")
        if provenance:
            with open(os.path.join(task, "PROVENANCE.md"), "w", encoding="utf-8") as fh:
                fh.write("fixture\n")
        for rel, content in (workspace or {}).items():
            path = os.path.join(task, "workspace", rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        for rel, content in (solution or {}).items():
            path = os.path.join(task, "solution", rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        for rel, content in (checker_data or {"expected.txt": "ok\n"}).items():
            path = os.path.join(task, "checker_data", rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        checker_path = os.path.join(task, "checker.sh")
        with open(checker_path, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(checker).lstrip())
        os.chmod(checker_path, os.stat(checker_path).st_mode | stat.S_IXUSR)
        return task

    def test_deterministic_fixture_passes_gate(self):
        task = self.make_task(
            "deterministic",
            """
            set -eu
            if [ "$(cat answer.txt 2>/dev/null || true)" = "ok" ]; then
              echo PASS
              exit 0
            fi
            echo FAIL missing answer
            exit 1
            """,
            solution={"answer.txt": "ok\n"},
        )
        result = admission_gate.gate(task, determinism_runs=3, stress=0)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["findings"], [])

    def test_coin_flip_checker_flunks_determinism(self):
        task = self.make_task(
            "coinflip",
            """
            set -eu
            count_file="$TASK_DIR/checker_data/count.txt"
            count=$(cat "$count_file")
            count=$((count + 1))
            printf '%s\n' "$count" > "$count_file"
            if [ $((count % 2)) -eq 0 ]; then
              echo PASS
              exit 0
            fi
            echo FAIL coin flip
            exit 1
            """,
            solution={"answer.txt": "ok\n"},
            checker_data={"count.txt": "0\n"},
        )
        result = determinism_check.run_determinism_check(task, runs=4, stress=0)
        self.assertFalse(result["pass"])
        self.assertIn("solution did not exit 0 on every run", result["findings"])

    def test_workspace_partial_credit_score_divergence_flunks_determinism(self):
        verdicts = [
            {"exit_code": 1, "score": 0.1, "first_fail_line": "FAIL same", "timed_out": False},
            {"exit_code": 1, "score": 0.2, "first_fail_line": "FAIL same", "timed_out": False},
        ]
        findings = determinism_check.evaluate([], verdicts)
        self.assertIn("workspace scores diverged: [0.1, 0.2]", findings)

    def test_run_command_timeout_returns_failed_verdict(self):
        code, output, _wall, timed_out = determinism_check.run_command(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=0.1,
        )
        self.assertEqual(code, 124)
        self.assertTrue(timed_out)
        self.assertIn("checker timed out", output)

    def test_checker_data_shell_helper_workspace_oracle_is_hard_failure(self):
        task = self.make_task(
            "shell-helper-workspace-oracle",
            """
            set -eu
            bash "$TASK_DIR/checker_data/helper.sh"
            if [ -f answer.txt ]; then
              echo PASS
              exit 0
            fi
            echo FAIL missing answer
            exit 1
            """,
            workspace={"oracle.py": "print('secret')\n"},
            solution={"answer.txt": "ok\n"},
            checker_data={"helper.sh": "python workspace/oracle.py >/dev/null 2>&1 || true\n"},
        )
        result = admission_gate.gate(task, determinism_runs=2, stress=0)
        rules = {finding["rule"] for finding in result["findings"]}
        self.assertIn("ownership.workspace_py_reference", rules)

    def test_shell_python_workspace_oracle_is_hard_failure(self):
        task = self.make_task(
            "shell-workspace-oracle",
            """
            set -eu
            python workspace/oracle.py >/dev/null 2>&1 || true
            if [ -f answer.txt ]; then
              echo PASS
              exit 0
            fi
            echo FAIL missing answer
            exit 1
            """,
            workspace={"oracle.py": "print('secret')\n"},
            solution={"answer.txt": "ok\n"},
        )
        result = admission_gate.gate(task, determinism_runs=2, stress=0)
        rules = {finding["rule"] for finding in result["findings"]}
        self.assertIn("ownership.workspace_py_reference", rules)

    def test_workspace_python_oracle_is_hard_failure(self):
        task = self.make_task(
            "workspace-oracle",
            """
            set -eu
            if [ -f answer.txt ]; then
              echo PASS
              exit 0
            fi
            echo FAIL missing answer
            exit 1
            """,
            workspace={"oracle.py": "EXPECTED = 'secret'\n"},
            solution={"answer.txt": "ok\n"},
            checker_data={
                "suite/scan.py": "import importlib.util\nspec = importlib.util.spec_from_file_location('oracle', 'workspace/oracle.py')\n"
            },
        )
        result = admission_gate.gate(task, determinism_runs=2, stress=0)
        self.assertEqual(result["status"], "FAIL")
        rules = {finding["rule"] for finding in result["findings"]}
        self.assertIn("ownership.workspace_py_reference", rules)


if __name__ == "__main__":
    unittest.main()
