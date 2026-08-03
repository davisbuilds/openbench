"""CLI contract tests for Harbor OAuth execution."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from obench import cli, harbor_cli, harbor_run


class HarborCliTests(unittest.TestCase):
    def _invoke(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = cli.main(["harbor", *argv])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_top_level_and_subcommand_help_are_public(self):
        top_level = subprocess.run(
            [sys.executable, "-m", "obench", "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(top_level.returncode, 0, top_level.stderr)
        self.assertIn("harbor", top_level.stdout)
        self.assertIn("run exported tasks through Harbor", top_level.stdout)

        command = subprocess.run(
            [sys.executable, "-m", "obench", "harbor", "oauth-run", "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(command.returncode, 0, command.stderr)
        for option in (
            "--task",
            "--model",
            "--master-auth-json",
            "--jobs-dir",
            "--job-name",
            "--harbor-binary",
        ):
            self.assertIn(option, command.stdout)
        self.assertIn("never command arguments or environment", command.stdout)

    def test_oauth_run_constructs_exact_library_call_without_credential_bytes(self):
        expected_job = Path("/tmp/jobs/oauth-smoke-001")
        result = harbor_run.HarborRunResult(
            returncode=0,
            plan=harbor_run.HarborRunPlan(
                argv=("/opt/harbor", "run"),
                task_path=Path("/tmp/exported-task"),
                expected_job_path=expected_job,
                harbor_version="0.20.0",
            ),
        )
        credential_bytes = "access-token-must-not-appear"
        argv = [
            "oauth-run",
            "--task",
            "/tmp/exported-task",
            "--model",
            "openai/gpt-5",
            "--master-auth-json",
            "/secure/auth.json",
            "--jobs-dir",
            "/tmp/jobs",
            "--job-name",
            "oauth-smoke-001",
            "--harbor-binary",
            "/opt/harbor",
        ]

        with mock.patch.object(harbor_cli, "run_harbor_oauth", return_value=result) as run:
            code, stdout, stderr = self._invoke(argv)

        self.assertEqual(code, 0)
        run.assert_called_once_with(
            task_dir="/tmp/exported-task",
            model="openai/gpt-5",
            master_auth_json="/secure/auth.json",
            jobs_dir="/tmp/jobs",
            job_name="oauth-smoke-001",
            harbor_binary="/opt/harbor",
            run_process=mock.ANY,
        )
        process_runner = run.call_args.kwargs["run_process"]
        self.assertIsInstance(process_runner, harbor_cli._ExitRecordingProcessRunner)
        self.assertIsNone(process_runner.harbor_returncode)
        self.assertEqual(
            stdout,
            "Harbor exited with code 0; "
            "expected job output: /tmp/jobs/oauth-smoke-001\n",
        )
        self.assertEqual(stderr, "")
        self.assertNotIn(credential_bytes, " ".join(argv) + stdout + stderr)

    def test_nonzero_runner_code_is_reported_and_propagated_exactly(self):
        result = harbor_run.HarborRunResult(
            returncode=37,
            plan=harbor_run.HarborRunPlan(
                argv=("harbor", "run"),
                task_path=Path("/tmp/exported-task"),
                expected_job_path=Path("/tmp/jobs/failed"),
                harbor_version="0.20.0",
            ),
        )
        argv = [
            "oauth-run",
            "--task=/tmp/exported-task",
            "--model=openai/gpt-5",
            "--master-auth-json=/secure/auth.json",
            "--jobs-dir=/tmp/jobs",
            "--job-name=failed",
        ]

        with mock.patch.object(harbor_cli, "run_harbor_oauth", return_value=result):
            code, stdout, stderr = self._invoke(argv)

        self.assertEqual(code, 37)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "Harbor exited with code 37; expected job output: /tmp/jobs/failed\n",
        )

    def test_known_preparation_error_is_concise_and_returns_two(self):
        argv = [
            "oauth-run",
            "--task=/tmp/not-exported",
            "--model=openai/gpt-5",
            "--master-auth-json=/secure/auth.json",
            "--jobs-dir=/tmp/jobs",
            "--job-name=invalid",
        ]
        with mock.patch.object(
            harbor_cli,
            "run_harbor_oauth",
            side_effect=harbor_run.HarborRunError("task contract rejected"),
        ):
            code, stdout, stderr = self._invoke(argv)

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "ERROR: Harbor OAuth run could not start: task contract rejected\n",
        )
        self.assertNotIn("Traceback", stderr)

    def test_post_run_oauth_error_reports_and_preserves_nonzero_harbor_code(self):
        argv = [
            "oauth-run",
            "--task=/tmp/exported-task",
            "--model=openai/gpt-5",
            "--master-auth-json=/secure/auth.json",
            "--jobs-dir=/tmp/jobs",
            "--job-name=missing-return",
        ]

        def fail_after_harbor(**kwargs):
            kwargs["run_process"].harbor_returncode = 9
            raise harbor_cli.HarborOAuthError("returned auth.json is missing")

        with mock.patch.object(
            harbor_cli,
            "run_harbor_oauth",
            side_effect=fail_after_harbor,
        ):
            code, stdout, stderr = self._invoke(argv)

        self.assertEqual(code, 9)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "ERROR: Harbor exited with code 9, but OAuth credential "
            "finalization failed: returned auth.json is missing\n",
        )

    def test_post_run_oauth_error_after_zero_exit_cannot_report_success(self):
        argv = [
            "oauth-run",
            "--task=/tmp/exported-task",
            "--model=openai/gpt-5",
            "--master-auth-json=/secure/auth.json",
            "--jobs-dir=/tmp/jobs",
            "--job-name=stale-auth",
        ]

        def fail_after_harbor(**kwargs):
            kwargs["run_process"].harbor_returncode = 0
            raise harbor_cli.HarborOAuthError("master auth.json changed")

        with mock.patch.object(
            harbor_cli,
            "run_harbor_oauth",
            side_effect=fail_after_harbor,
        ):
            code, stdout, stderr = self._invoke(argv)

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Harbor exited with code 0", stderr)
        self.assertIn("credential finalization failed", stderr)


if __name__ == "__main__":
    unittest.main()
