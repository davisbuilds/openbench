"""Offline tests for the fixed one-trial Harbor OAuth runner."""

from __future__ import annotations

import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from obench import harbor_oauth, harbor_run


OLD_AUTH = b'{"account_id":"owner","tokens":{"access_token":"old","refresh_token":"old-r"}}'
ROTATED_AUTH = b'{"account_id":"owner","tokens":{"access_token":"new","refresh_token":"new-r"}}'
NEWER_AUTH = b'{"account_id":"owner","tokens":{"access_token":"newer","refresh_token":"newer-r"}}'


def _write_private(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def _agent_env(argv: list[str]) -> dict[str, str]:
    result = {}
    for index, item in enumerate(argv):
        if item == "--ae":
            key, value = argv[index + 1].split("=", 1)
            result[key] = value
    return result


class FakeProcessRunner:
    def __init__(
        self,
        rotations: list[bytes | None],
        *,
        run_returncodes: list[int] | None = None,
        version: str = "0.20.0",
    ):
        self.rotations = list(rotations)
        self.run_returncodes = list(run_returncodes or [0] * len(rotations))
        self.version = version
        self.calls: list[tuple[list[str], dict]] = []
        self.staged_inputs: list[bytes] = []
        self.stage_dirs: list[Path] = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, dict(kwargs)))
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(
                argv, 0, stdout=f"{self.version}\n", stderr=""
            )

        env = _agent_env(argv)
        input_path = Path(env[harbor_oauth.CODEX_AUTH_JSON_PATH])
        return_path = Path(env[harbor_oauth.CODEX_AUTH_RETURN_PATH])
        self.staged_inputs.append(input_path.read_bytes())
        self.stage_dirs.append(input_path.parent)
        rotation = self.rotations.pop(0)
        returncode = self.run_returncodes.pop(0)
        if rotation is not None:
            _write_private(return_path, rotation)
        return subprocess.CompletedProcess(argv, returncode)


class HarborRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.task = self.root / "task"
        self.task.mkdir()
        (self.task / "task.toml").write_text(
            'schema_version = "1.4"\n'
            '\n[task]\nname = "openbench/example"\n'
            '\n[metadata]\norigin = "openbench"\n'
            '\n[[artifacts]]\nsource = "/app"\ndestination = "workspace"\n',
            encoding="utf-8",
        )
        (self.task / "instruction.md").write_text("Do the task.\n", encoding="utf-8")
        self.master = self.root / "auth.json"
        _write_private(self.master, OLD_AUTH)
        self.jobs = self.root / "jobs"
        self.harbor = self.root / "harbor"
        self.harbor.write_text("#!/bin/sh\n", encoding="utf-8")
        self.harbor.chmod(0o700)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, runner, *, job_name="oauth-one", **overrides):
        arguments = {
            "task_dir": self.task,
            "model": "openai/gpt-5",
            "master_auth_json": self.master,
            "jobs_dir": self.jobs,
            "job_name": job_name,
            "harbor_binary": self.harbor,
            "run_process": runner,
        }
        arguments.update(overrides)
        return harbor_run.run_harbor_oauth(**arguments)

    def test_exact_argv_is_path_only_and_expected_job_is_discoverable(self):
        runner = FakeProcessRunner([ROTATED_AUTH])

        result = self._run(runner)

        stage_dir = runner.stage_dirs[0]
        expected = [
            str(self.harbor.resolve()),
            "run",
            "-p",
            str(self.task.resolve()),
            "-a",
            harbor_oauth.AGENT_IMPORT_PATH,
            "-m",
            "openai/gpt-5",
            "-k",
            "1",
            "-n",
            "1",
            "-r",
            "0",
            "-o",
            str(self.jobs.resolve()),
            "--job-name",
            "oauth-one",
            "--ae",
            f"{harbor_oauth.CODEX_AUTH_JSON_PATH}={stage_dir / 'auth.json'}",
            "--ae",
            f"{harbor_oauth.CODEX_AUTH_RETURN_PATH}={stage_dir / 'auth-return.json'}",
        ]
        self.assertEqual(runner.calls[1][0], expected)
        self.assertEqual(result.plan.argv, tuple(expected))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.expected_job_path, self.jobs.resolve() / "oauth-one")
        self.assertFalse(stage_dir.exists())
        self.assertEqual(self.master.read_bytes(), ROTATED_AUTH)

        loggable = json.dumps(
            {
                "argv": runner.calls[1][0],
                "kwargs": runner.calls[1][1],
                "plan": result.plan.argv,
            }
        )
        for secret in ("old-r", "new-r", '"access_token":"old"', '"access_token":"new"'):
            self.assertNotIn(secret, loggable)
        self.assertNotIn("env", runner.calls[1][1])

    def test_nonzero_harbor_exit_is_preserved_after_rotation_and_cleanup(self):
        runner = FakeProcessRunner([ROTATED_AUTH], run_returncodes=[37])

        result = self._run(runner)

        self.assertEqual(result.returncode, 37)
        self.assertEqual(self.master.read_bytes(), ROTATED_AUTH)
        self.assertFalse(runner.stage_dirs[0].exists())

    def test_missing_return_fails_closed_and_cleans_staging(self):
        runner = FakeProcessRunner([None], run_returncodes=[9])

        with self.assertRaises(harbor_oauth.MissingAuthReturnError):
            self._run(runner)

        self.assertEqual(self.master.read_bytes(), OLD_AUTH)
        self.assertFalse(runner.stage_dirs[0].exists())

    def test_unsupported_agent_lifecycle_fails_closed_and_cleans_staging(self):
        class UnsupportedRunner(FakeProcessRunner):
            def __call__(self, argv, **kwargs):
                if list(argv)[-1] == "--version":
                    return super().__call__(argv, **kwargs)
                env = _agent_env(list(argv))
                stage_dir = Path(env[harbor_oauth.CODEX_AUTH_JSON_PATH]).parent
                self.stage_dirs.append(stage_dir)
                raise harbor_oauth.HarborOAuthUnsupportedError(
                    "synthetic unsupported cleanup"
                )

        runner = UnsupportedRunner([])
        with self.assertRaises(harbor_oauth.HarborOAuthUnsupportedError):
            self._run(runner)
        self.assertEqual(self.master.read_bytes(), OLD_AUTH)
        self.assertFalse(runner.stage_dirs[0].exists())

    def test_version_preflight_happens_before_credential_staging(self):
        runner = FakeProcessRunner([], version="0.19.0")
        with mock.patch.object(
            harbor_run.HarborOAuthCredential,
            "__enter__",
            side_effect=AssertionError("credential was staged"),
        ) as enter:
            with self.assertRaisesRegex(harbor_run.HarborRunError, "0.20.0"):
                self._run(runner)
        enter.assert_not_called()
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(self.jobs.exists())

    def test_two_sequential_runs_stage_the_first_rotation(self):
        runner = FakeProcessRunner([ROTATED_AUTH, NEWER_AUTH])

        first = self._run(runner, job_name="first")
        second = self._run(runner, job_name="second")

        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(runner.staged_inputs, [OLD_AUTH, ROTATED_AUTH])
        self.assertEqual(self.master.read_bytes(), NEWER_AUTH)
        self.assertTrue(all(not path.exists() for path in runner.stage_dirs))

    def test_rejects_dataset_root_with_nested_tasks(self):
        dataset = self.root / "dataset"
        dataset.mkdir()
        (dataset / "task.toml").write_text(
            '[task]\nname = "parent"\n', encoding="utf-8"
        )
        (dataset / "instruction.md").write_text("Parent\n", encoding="utf-8")
        nested = dataset / "child"
        nested.mkdir()
        (nested / "task.toml").write_text(
            '[task]\nname = "child"\n', encoding="utf-8"
        )

        with self.assertRaisesRegex(harbor_run.HarborRunError, "nested task.toml"):
            harbor_run.validate_task_root(dataset)

    def test_rejects_symlinked_jobs_dir_before_preflight_or_staging(self):
        real_jobs = self.root / "real-jobs"
        real_jobs.mkdir()
        self.jobs.symlink_to(real_jobs, target_is_directory=True)
        runner = FakeProcessRunner([])

        with mock.patch.object(
            harbor_run.HarborOAuthCredential,
            "__enter__",
            side_effect=AssertionError("credential was staged"),
        ) as enter:
            with self.assertRaisesRegex(harbor_run.HarborRunError, "symlink"):
                self._run(runner)

        enter.assert_not_called()
        self.assertEqual(runner.calls, [])

    def test_rejects_non_exporter_task_contracts(self):
        invalid_contracts = {
            "legacy schema": (
                'schema_version = "1.3"\n'
                '[task]\nname = "openbench/example"\n'
                '[metadata]\norigin = "openbench"\n'
                '[[artifacts]]\nsource = "/app"\ndestination = "workspace"\n'
            ),
            "foreign origin": (
                'schema_version = "1.4"\n'
                '[task]\nname = "openbench/example"\n'
                '[metadata]\norigin = "other"\n'
                '[[artifacts]]\nsource = "/app"\ndestination = "workspace"\n'
            ),
            "missing artifact": (
                'schema_version = "1.4"\n'
                '[task]\nname = "openbench/example"\n'
                '[metadata]\norigin = "openbench"\n'
            ),
            "wrong artifact": (
                'schema_version = "1.4"\n'
                '[task]\nname = "openbench/example"\n'
                '[metadata]\norigin = "openbench"\n'
                '[[artifacts]]\nsource = "/tmp"\ndestination = "workspace"\n'
            ),
            "multiple artifacts": (
                'schema_version = "1.4"\n'
                '[task]\nname = "openbench/example"\n'
                '[metadata]\norigin = "openbench"\n'
                '[[artifacts]]\nsource = "/app"\ndestination = "workspace"\n'
                '[[artifacts]]\nsource = "/logs"\ndestination = "logs"\n'
            ),
            "artifact extra field": (
                'schema_version = "1.4"\n'
                '[task]\nname = "openbench/example"\n'
                '[metadata]\norigin = "openbench"\n'
                '[[artifacts]]\nsource = "/app"\ndestination = "workspace"\n'
                'required = true\n'
            ),
        }

        for name, task_toml in invalid_contracts.items():
            with self.subTest(name=name):
                (self.task / "task.toml").write_text(task_toml, encoding="utf-8")
                with self.assertRaises(harbor_run.HarborRunError):
                    harbor_run.validate_task_root(self.task)

    def test_staged_paths_are_private(self):
        class PermissionRunner(FakeProcessRunner):
            def __call__(self, argv, **kwargs):
                if list(argv)[-1] == "--version":
                    return super().__call__(argv, **kwargs)
                env = _agent_env(list(argv))
                auth_path = Path(env[harbor_oauth.CODEX_AUTH_JSON_PATH])
                self.assertions = (
                    stat.S_IMODE(auth_path.parent.stat().st_mode),
                    stat.S_IMODE(auth_path.stat().st_mode),
                )
                return super().__call__(argv, **kwargs)

        runner = PermissionRunner([ROTATED_AUTH])
        self._run(runner)
        self.assertEqual(runner.assertions, (0o700, 0o600))


if __name__ == "__main__":
    unittest.main()
