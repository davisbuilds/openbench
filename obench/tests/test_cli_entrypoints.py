"""Coverage for package and task-admission umbrella entry points."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock

from obench import cli


class CliEntrypointTests(unittest.TestCase):
    def test_python_m_obench_reports_version(self):
        repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        proc = subprocess.run(
            [sys.executable, "-m", "obench", "--version"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(proc.stdout, r"^obench \S+")

    def test_admit_routes_to_task_admission_gate(self):
        with mock.patch(
            "obench.admission_gate.main", return_value=7
        ) as admission_main:
            self.assertEqual(cli.main(["admit", "tasks/demo"]), 7)
        admission_main.assert_called_once_with(["tasks/demo"])
