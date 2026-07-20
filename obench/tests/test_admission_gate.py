#!/usr/bin/env python3
"""Tests for pre-admission task gates."""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest


from obench import admission_gate  # noqa: E402
from obench import determinism_check  # noqa: E402


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


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

    def test_timing_sensitive_checker_is_hard_failure(self):
        task = self.make_task(
            "timing-sensitive",
            """
            set -eu
            python3 "$TASK_DIR/checker_data/check.py"
            """,
            solution={"answer.txt": "ok\n"},
            checker_data={
                "check.py": "import subprocess\nsubprocess.run(['true'], timeout=5)\nprint('PASS')\n"
            },
        )
        result = admission_gate.gate(task, determinism_runs=2, stress=0)
        self.assertEqual(result["status"], "FAIL")
        findings = {(f["rule"], f["level"]) for f in result["findings"]}
        self.assertIn(("timing_sensitivity", "hard"), findings)

    def test_self_contained_checker_without_checker_data_passes(self):
        task = self.make_task(
            "self-contained",
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
        shutil.rmtree(os.path.join(task, "checker_data"))
        result = admission_gate.gate(task, determinism_runs=2, stress=0)
        self.assertEqual(result["status"], "PASS")

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


    def test_git_mode_workspace_oracle_is_hard_failure(self):
        """Ownership scan must materialize workspace.toml trees, not skip them."""
        repo = os.path.join(self.tmp, "git-repo")
        os.makedirs(repo)
        _git(repo, "init")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")
        _git(repo, "checkout", "-b", "main")
        with open(os.path.join(repo, "oracle.py"), "w", encoding="utf-8") as fh:
            fh.write("EXPECTED = 'secret'\n")
        with open(os.path.join(repo, "stub.txt"), "w", encoding="utf-8") as fh:
            fh.write("start\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "initial")
        sha = subprocess.check_output(
            ["git", "-C", repo, "rev-parse", "HEAD"], text=True,
        ).strip()

        task = os.path.join(repo, ".openbench", "tasks", "git-oracle")
        os.makedirs(os.path.join(task, "solution"))
        os.makedirs(os.path.join(task, "checker_data"))
        with open(os.path.join(task, "instruction.md"), "w", encoding="utf-8") as fh:
            fh.write("Do the fixture task.\n")
        with open(os.path.join(task, "PROVENANCE.md"), "w", encoding="utf-8") as fh:
            fh.write("fixture\n")
        with open(os.path.join(task, "workspace.toml"), "w", encoding="utf-8") as fh:
            fh.write(f'kind = "git"\nrepo = "."\nref = "{sha}"\n')
        with open(os.path.join(task, "solution", "answer.txt"), "w", encoding="utf-8") as fh:
            fh.write("ok\n")
        with open(os.path.join(task, "checker_data", "expected.txt"), "w", encoding="utf-8") as fh:
            fh.write("ok\n")
        checker = textwrap.dedent("""\
            #!/usr/bin/env bash
            set -eu
            python workspace/oracle.py >/dev/null 2>&1 || true
            if [ -f answer.txt ]; then
              echo PASS
              exit 0
            fi
            echo FAIL missing answer
            exit 1
            """)
        checker_path = os.path.join(task, "checker.sh")
        with open(checker_path, "w", encoding="utf-8") as fh:
            fh.write(checker)
        os.chmod(checker_path, os.stat(checker_path).st_mode | stat.S_IXUSR)

        findings = admission_gate.scan_ownership(task)
        rules = {f.rule for f in findings}
        self.assertIn("ownership.workspace_py_reference", rules)


if __name__ == "__main__":
    unittest.main()
