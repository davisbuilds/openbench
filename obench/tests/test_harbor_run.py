"""Offline tests for the fixed one-trial Harbor OAuth runner."""

from __future__ import annotations

from dataclasses import replace
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from obench import harbor_oauth, harbor_profiles, harbor_run


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


def _is_metadata_preflight(argv: list[str]) -> bool:
    return len(argv) > 1 and argv[1] == "-c"


def _metadata_result(
    argv: list[str],
    *,
    commit: str = harbor_run.HARBOR_GIT_COMMIT,
    editable: bool = False,
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        argv,
        0,
        stdout=json.dumps(
            {
                "version": "0.20.0",
                "git_commit": commit,
                "is_editable": editable,
            }
        )
        + "\n",
        stderr="",
    )


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
        if _is_metadata_preflight(argv):
            return _metadata_result(argv)

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
            '\n[environment]\nnetwork_mode = "public"\n'
            '\n[[artifacts]]\nsource = "/app"\ndestination = "workspace"\n',
            encoding="utf-8",
        )
        (self.task / "instruction.md").write_text("Do the task.\n", encoding="utf-8")
        self.master = self.root / "auth.json"
        _write_private(self.master, OLD_AUTH)
        self.jobs = self.root / "jobs"
        self.harbor = self.root / "harbor"
        self.harbor.write_text(f"#!{sys.executable}\n", encoding="utf-8")
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
        self.assertEqual(runner.calls[2][0], expected)
        self.assertEqual(result.plan.argv, tuple(expected))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.expected_job_path, self.jobs.resolve() / "oauth-one")
        self.assertFalse(stage_dir.exists())
        self.assertEqual(self.master.read_bytes(), ROTATED_AUTH)

        loggable = json.dumps(
            {
                "argv": runner.calls[2][0],
                "kwargs": runner.calls[2][1],
                "plan": result.plan.argv,
            }
        )
        for secret in ("old-r", "new-r", '"access_token":"old"', '"access_token":"new"'):
            self.assertNotIn(secret, loggable)
        self.assertNotIn("env", runner.calls[2][1])

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
                if (
                    list(argv)[-1] == "--version"
                    or _is_metadata_preflight(list(argv))
                ):
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

    def test_commit_preflight_happens_before_credential_staging(self):
        class WrongCommitRunner(FakeProcessRunner):
            def __call__(self, argv, **kwargs):
                argv = list(argv)
                if _is_metadata_preflight(argv):
                    self.calls.append((argv, dict(kwargs)))
                    return _metadata_result(argv, commit="0" * 40)
                return super().__call__(argv, **kwargs)

        runner = WrongCommitRunner([])
        with mock.patch.object(
            harbor_run.HarborOAuthCredential,
            "__enter__",
            side_effect=AssertionError("credential was staged"),
        ) as enter:
            with self.assertRaisesRegex(
                harbor_run.HarborRunError, "provenance mismatch"
            ):
                self._run(runner)
        enter.assert_not_called()
        self.assertEqual(len(runner.calls), 2)

    def test_no_network_fails_before_binary_or_credential_preflight(self):
        task_toml = self.task / "task.toml"
        task_toml.write_text(
            task_toml.read_text(encoding="utf-8").replace(
                'network_mode = "public"',
                'network_mode = "no-network"',
            ),
            encoding="utf-8",
        )
        runner = FakeProcessRunner([])
        with mock.patch.object(
            harbor_run.HarborOAuthCredential,
            "__enter__",
            side_effect=AssertionError("credential was staged"),
        ) as enter:
            with self.assertRaisesRegex(
                harbor_run.HarborRunError,
                "requires public agent networking",
            ):
                self._run(runner)
        enter.assert_not_called()
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.jobs.exists())

    def test_agent_network_override_is_effective(self):
        task_toml = self.task / "task.toml"
        public_task = task_toml.read_text(encoding="utf-8")
        task_toml.write_text(
            public_task + '\n[agent]\nnetwork_mode = "no-network"\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            harbor_run.HarborRunError,
            "requires public agent networking",
        ):
            harbor_run.validate_task_root(self.task)

        task_toml.write_text(
            public_task.replace(
                'network_mode = "public"',
                'network_mode = "no-network"',
            )
            + '\n[agent]\nnetwork_mode = "public"\n',
            encoding="utf-8",
        )
        self.assertEqual(
            harbor_run.validate_task_root(self.task),
            self.task.resolve(),
        )

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
                if (
                    list(argv)[-1] == "--version"
                    or _is_metadata_preflight(list(argv))
                ):
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

    def test_profile_job_runs_native_matrix_with_separate_oauth_leases(self):
        exports = self.root / "exports"
        for name in ("make-it-run", "fix-failing-test"):
            task = exports / name
            task.mkdir(parents=True)
            (task / "task.toml").write_text(
                (self.task / "task.toml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (task / "instruction.md").write_text("Task.\n", encoding="utf-8")
        codex_auth = self.root / "codex-auth.json"
        pi_auth = self.root / "pi-auth.json"
        _write_private(codex_auth, OLD_AUTH)
        _write_private(pi_auth, b'{"openai-codex":{"type":"oauth"}}')
        config = self.root / "profile-job.json"
        stage_dirs: list[Path] = []

        def profile(harness: str, model: str):
            resolved = harbor_profiles.resolve_harbor_profile(harness, model)
            source = codex_auth if harness == "codex" else pi_auth
            return replace(
                resolved,
                auth=replace(
                    resolved.auth,
                    source_candidates=(str(source),),
                ),
            )

        def runner(argv, **kwargs):
            argv = list(argv)
            if argv[-1] == "--version":
                return subprocess.CompletedProcess(
                    argv, 0, stdout="0.20.0\n", stderr=""
                )
            if _is_metadata_preflight(argv):
                return _metadata_result(argv)
            env = kwargs["env"]
            for harness in ("CODEX", "PI"):
                input_path = Path(
                    env[f"OPENBENCH_HARBOR_{harness}_AUTH_INPUT"]
                )
                return_path = Path(
                    env[f"OPENBENCH_HARBOR_{harness}_AUTH_RETURN"]
                )
                stage_dirs.append(input_path.parent)
                _write_private(return_path, input_path.read_bytes())
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(
            harbor_run,
            "resolve_harbor_profile",
            side_effect=profile,
        ):
            result = harbor_run.run_harbor_profile_job(
                exported_tasks_dir=exports,
                task_names=("make-it-run", "fix-failing-test"),
                harnesses=("codex", "pi"),
                model="gpt-5.6-sol",
                attempts=2,
                n_concurrent_trials=2,
                max_retries=1,
                jobs_dir=self.jobs,
                job_name="profile-job",
                config_path=config,
                harbor_binary=self.harbor,
                run_process=runner,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.artifact.trial_count, 8)
        self.assertEqual(
            result.expected_job_path,
            result.artifact.jobs_dir / "profile-job",
        )
        rendered = result.artifact.as_dict()
        self.assertEqual(
            [agent["model_name"] for agent in rendered["agents"]],
            ["gpt-5.6-sol", "openai-codex/gpt-5.6-sol"],
        )
        self.assertEqual(
            rendered["datasets"][0]["task_names"],
            ["fix-failing-test", "make-it-run"],
        )
        serialized = result.artifact.json_bytes
        self.assertNotIn(OLD_AUTH, serialized)
        self.assertNotIn(pi_auth.read_bytes(), serialized)
        self.assertEqual(codex_auth.read_bytes(), OLD_AUTH)
        self.assertEqual(pi_auth.read_bytes(), b'{"openai-codex":{"type":"oauth"}}')
        self.assertTrue(all(not path.exists() for path in stage_dirs))

    def test_profile_job_noop_resume_persists_unchanged_credentials(self):
        exports = self.root / "exports"
        task = exports / "make-it-run"
        task.mkdir(parents=True)
        (task / "task.toml").write_text(
            (self.task / "task.toml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (task / "instruction.md").write_text("Task.\n", encoding="utf-8")
        codex_auth = self.root / "codex-auth.json"
        _write_private(codex_auth, OLD_AUTH)
        expected_job = self.jobs / "profile-resume"
        expected_job.mkdir(parents=True)
        (expected_job / "config.json").write_text("{}\n", encoding="utf-8")
        observed_return: list[bytes] = []

        def profile(harness: str, model: str):
            resolved = harbor_profiles.resolve_harbor_profile(harness, model)
            return replace(
                resolved,
                auth=replace(
                    resolved.auth,
                    source_candidates=(str(codex_auth),),
                ),
            )

        def runner(argv, **kwargs):
            argv = list(argv)
            if argv[-1] == "--version":
                return subprocess.CompletedProcess(
                    argv, 0, stdout="0.20.0\n", stderr=""
                )
            if _is_metadata_preflight(argv):
                return _metadata_result(argv)
            env = kwargs["env"]
            return_path = Path(env["OPENBENCH_HARBOR_CODEX_AUTH_RETURN"])
            observed_return.append(return_path.read_bytes())
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(
            harbor_run,
            "resolve_harbor_profile",
            side_effect=profile,
        ):
            result = harbor_run.run_harbor_profile_job(
                exported_tasks_dir=exports,
                task_names=("make-it-run",),
                harnesses=("codex",),
                model="gpt-5.6-sol",
                attempts=1,
                n_concurrent_trials=1,
                max_retries=0,
                jobs_dir=self.jobs,
                job_name="profile-resume",
                config_path=self.root / "profile-resume.json",
                harbor_binary=self.harbor,
                run_process=runner,
            )

        self.assertTrue(result.resumes_existing_job)
        self.assertEqual(observed_return, [OLD_AUTH])
        self.assertEqual(codex_auth.read_bytes(), OLD_AUTH)

    def test_subscription_profile_stages_read_only_archive_without_return(self):
        exports = self.root / "exports"
        task = exports / "make-it-run"
        task.mkdir(parents=True)
        (task / "task.toml").write_text(
            (self.task / "task.toml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (task / "instruction.md").write_text("Task.\n", encoding="utf-8")
        cursor_auth = self.root / "auth.json"
        _write_private(cursor_auth, b'{"accessToken":"cursor-secret"}')
        observed_archive: list[Path] = []

        def profile(harness: str, model: str):
            resolved = harbor_profiles.resolve_harbor_profile(harness, model)
            return replace(
                resolved,
                auth=replace(
                    resolved.auth,
                    source_candidates=(str(cursor_auth),),
                ),
            )

        def runner(argv, **kwargs):
            argv = list(argv)
            if argv[-1] == "--version":
                return subprocess.CompletedProcess(
                    argv, 0, stdout="0.20.0\n", stderr=""
                )
            if _is_metadata_preflight(argv):
                return _metadata_result(argv)
            env = kwargs["env"]
            archive = Path(env["OPENBENCH_HARBOR_CURSOR_AUTH_INPUT"])
            observed_archive.append(archive)
            self.assertTrue(archive.is_file())
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
            self.assertNotIn(
                "OPENBENCH_HARBOR_CURSOR_AUTH_RETURN", env
            )
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(
            harbor_run,
            "resolve_harbor_profile",
            side_effect=profile,
        ):
            result = harbor_run.run_harbor_profile_job(
                exported_tasks_dir=exports,
                task_names=("make-it-run",),
                harnesses=("cursor",),
                model="gpt-5.6-sol",
                attempts=1,
                n_concurrent_trials=1,
                max_retries=0,
                jobs_dir=self.jobs,
                job_name="cursor-profile",
                config_path=self.root / "cursor-profile.json",
                harbor_binary=self.harbor,
                run_process=runner,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(observed_archive), 1)
        self.assertFalse(observed_archive[0].exists())
        agent = result.artifact.as_dict()["agents"][0]
        self.assertEqual(agent["model_name"], "gpt-5.6-sol")
        self.assertEqual(
            agent["env"],
            {
                "OPENBENCH_CURSOR_AUTH_ARCHIVE": (
                    "${OPENBENCH_HARBOR_CURSOR_AUTH_INPUT}"
                )
            },
        )
        self.assertEqual(
            cursor_auth.read_bytes(), b'{"accessToken":"cursor-secret"}'
        )


if __name__ == "__main__":
    unittest.main()
