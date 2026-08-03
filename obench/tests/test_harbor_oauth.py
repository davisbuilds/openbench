"""Offline contract tests for the optional Harbor Codex OAuth bridge."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import PurePosixPath
import shutil
import stat
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

from obench import auth_persist
from obench import harbor_oauth
from obench.harbor_agents import codex as harbor_codex_agent


OLD_AUTH = b'{"account_id":"owner","tokens":{"access_token":"old","refresh_token":"old-r"}}'
ROTATED_AUTH = b'{"account_id":"owner","tokens":{"access_token":"new","refresh_token":"new-r"}}'
NEWER_AUTH = b'{"account_id":"owner","tokens":{"access_token":"newer","refresh_token":"newer-r"}}'
OTHER_ACCOUNT_AUTH = b'{"account_id":"other","tokens":{"access_token":"new","refresh_token":"new-r"}}'


class FakeEnvironment:
    """In-memory Harbor environment surface; credential bytes stay remote."""

    default_user = "agent"

    def __init__(self, rotated_auth=ROTATED_AUTH, *, agent_failure=False,
                 download_failure=False):
        self.rotated_auth = rotated_auth
        self.agent_failure = agent_failure
        self.download_failure = download_failure
        self.remote_files = {}
        self.commands = []
        self.uploads = []
        self.downloads = []
        self.logs = []
        self.artifacts = []
        self.events = []

    async def upload_file(self, source_path, target_path):
        source = os.fspath(source_path)
        self.uploads.append((source, target_path))
        self.events.append(("upload", target_path))
        with open(source, "rb") as fh:
            self.remote_files[target_path] = fh.read()

    async def download_file(self, source_path, target_path):
        self.downloads.append((source_path, os.fspath(target_path)))
        self.events.append(("download", source_path))
        if self.download_failure:
            raise OSError("synthetic download failure")
        with open(target_path, "wb") as fh:
            fh.write(self.remote_files[source_path])

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.events.append(("exec", command))
        self.commands.append({
            "command": command,
            "cwd": cwd,
            "env": dict(env or {}),
            "timeout_sec": timeout_sec,
            "user": user,
        })
        if "codex exec" in command:
            self.remote_files["/tmp/codex-secrets/auth.json"] = self.rotated_auth
            if self.agent_failure:
                raise RuntimeError("synthetic agent failure")
        if "rm -rf" in command and "/tmp/codex-secrets" in command:
            self.remote_files.clear()
        return SimpleNamespace(return_code=0, stdout="", stderr="")


class FakeHarborCodex:
    """Current Harbor Codex lifecycle shape without importing Harbor."""

    _REMOTE_CODEX_HOME = PurePosixPath("/tmp/codex-home")
    _REMOTE_CODEX_SECRETS_DIR = PurePosixPath("/tmp/codex-secrets")
    emit_supported_cleanup = True

    def __init__(self, *, extra_env=None, **_kwargs):
        self._extra_env = dict(extra_env or {})

    def _get_env(self, key):
        return self._extra_env.get(key)

    async def exec_as_agent(self, environment, command, env=None, cwd=None,
                            timeout_sec=None):
        return await environment.exec(
            command,
            env=env,
            cwd=cwd,
            timeout_sec=timeout_sec,
            user=environment.default_user,
        )

    async def run(self, instruction, environment, context):
        del instruction, context
        auth_path = self._get_env(harbor_oauth.CODEX_AUTH_JSON_PATH)
        await environment.upload_file(
            auth_path, "/tmp/codex-secrets/auth.json"
        )
        try:
            await self.exec_as_agent(environment, "codex exec --json")
        finally:
            if self.emit_supported_cleanup:
                try:
                    await self.exec_as_agent(
                        environment,
                        'rm -rf /tmp/codex-secrets "$CODEX_HOME"',
                    )
                except Exception:
                    # Mirrors Harbor's best-effort cleanup block.
                    pass


class ChangedCleanupHarborCodex(FakeHarborCodex):
    emit_supported_cleanup = False

    async def run(self, instruction, environment, context):
        del instruction, context
        auth_path = self._get_env(harbor_oauth.CODEX_AUTH_JSON_PATH)
        await environment.upload_file(
            auth_path, "/tmp/codex-secrets/auth.json"
        )
        await self.exec_as_agent(environment, "codex exec --json")
        await self.exec_as_agent(environment, "cleanup-secrets-v2")


def _write(path, content):
    with open(path, "wb") as fh:
        fh.write(content)
    os.chmod(path, 0o600)


def _copy_input_to_return(config):
    shutil.copyfile(config.auth_json_path, config.auth_return_path)
    os.chmod(config.auth_return_path, 0o600)


class HostOAuthContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.master = os.path.join(self.tmp.name, "auth.json")
        _write(self.master, OLD_AUTH)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stages_only_auth_json_with_private_permissions_and_path_only_config(self):
        with harbor_oauth.build_harbor_oauth_context(self.master) as credential:
            config = credential.config
            stage_dir = os.path.dirname(config.auth_json_path)
            self.assertEqual(os.listdir(stage_dir), ["auth.json"])
            self.assertEqual(
                stat.S_IMODE(os.stat(stage_dir).st_mode), 0o700
            )
            self.assertEqual(
                stat.S_IMODE(os.stat(config.auth_json_path).st_mode), 0o600
            )
            rendered = json.dumps({
                "config": config.agent_extra_env(),
                "kwargs": config.agent_kwargs(),
            })
            self.assertNotIn("old-r", rendered)
            self.assertEqual(config.n_concurrent_trials, 1)
            self.assertEqual(config.max_retries, 0)
            self.assertEqual(
                config.agent_import_path, harbor_oauth.AGENT_IMPORT_PATH
            )
            _copy_input_to_return(config)
        self.assertFalse(os.path.exists(stage_dir))

    def test_rejects_second_concurrent_context_for_same_credential(self):
        first = harbor_oauth.build_harbor_oauth_context(self.master)
        second = harbor_oauth.build_harbor_oauth_context(self.master)
        with first as active:
            with self.assertRaises(harbor_oauth.ConcurrentCredentialUseError):
                second.__enter__()
            _copy_input_to_return(active.config)

    def test_rejects_harbor_while_native_consumer_owns_shared_lease(self):
        with auth_persist.auth_file_lease(self.master):
            with self.assertRaises(harbor_oauth.ConcurrentCredentialUseError):
                with harbor_oauth.build_harbor_oauth_context(self.master):
                    self.fail("Harbor unexpectedly acquired the native lease")

    def test_harbor_lease_blocks_other_shared_consumer(self):
        with harbor_oauth.build_harbor_oauth_context(self.master) as active:
            with self.assertRaises(
                auth_persist.CredentialLeaseUnavailableError
            ):
                with auth_persist.auth_file_lease(
                    self.master, blocking=False
                ):
                    self.fail("native consumer unexpectedly acquired Harbor lease")
            _copy_input_to_return(active.config)

    def test_cas_rejects_stale_rotation_without_overwriting_newer_master(self):
        credential = harbor_oauth.build_harbor_oauth_context(self.master)
        with self.assertRaises(harbor_oauth.StaleCredentialError):
            with credential as active:
                _write(active.config.auth_return_path, ROTATED_AUTH)
                _write(self.master, NEWER_AUTH)
        with open(self.master, "rb") as fh:
            self.assertEqual(fh.read(), NEWER_AUTH)

    def test_existing_identity_validator_rejects_account_change(self):
        with self.assertRaises(ValueError):
            with harbor_oauth.build_harbor_oauth_context(self.master) as active:
                _write(active.config.auth_return_path, OTHER_ACCOUNT_AUTH)
        with open(self.master, "rb") as fh:
            self.assertEqual(fh.read(), OLD_AUTH)

    def test_second_context_stages_rotation_returned_by_first_context(self):
        with harbor_oauth.build_harbor_oauth_context(self.master) as first:
            _write(first.config.auth_return_path, ROTATED_AUTH)
        with harbor_oauth.build_harbor_oauth_context(self.master) as second:
            with open(second.config.auth_json_path, "rb") as fh:
                self.assertEqual(fh.read(), ROTATED_AUTH)
            _write(second.config.auth_return_path, NEWER_AUTH)
        with open(self.master, "rb") as fh:
            self.assertEqual(fh.read(), NEWER_AUTH)

    def test_missing_return_fails_closed_and_cleans_staging(self):
        credential = harbor_oauth.build_harbor_oauth_context(self.master)
        with self.assertRaises(harbor_oauth.MissingAuthReturnError):
            with credential as active:
                stage_dir = os.path.dirname(active.config.auth_json_path)
        self.assertFalse(os.path.exists(stage_dir))
        with open(self.master, "rb") as fh:
            self.assertEqual(fh.read(), OLD_AUTH)


class HarborAgentHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.master = os.path.join(self.tmp.name, "auth.json")
        _write(self.master, OLD_AUTH)

    def tearDown(self):
        self.tmp.cleanup()

    async def _run(self, environment, base=FakeHarborCodex):
        agent_class = harbor_codex_agent._build_agent_class(base)
        with harbor_oauth.build_harbor_oauth_context(self.master) as credential:
            config = credential.config
            stage_dir = os.path.dirname(config.auth_json_path)
            agent = agent_class(**config.agent_kwargs())
            result = await agent.run("instruction", environment, SimpleNamespace())
            return result, config, stage_dir

    async def test_setup_exec_before_run_does_not_enter_oauth_capture(self):
        environment = FakeEnvironment()
        agent_class = harbor_codex_agent._build_agent_class(FakeHarborCodex)
        agent = agent_class(extra_env={})

        result = await agent.exec_as_agent(
            environment,
            "apt-get update && apt-get install -y nodejs",
        )

        self.assertEqual(result.return_code, 0)
        self.assertFalse(agent._oauth_run_active)
        self.assertFalse(agent._oauth_capture_attempted)
        self.assertEqual(environment.downloads, [])

    async def test_success_returns_rotation_before_cleanup_and_persists_it(self):
        environment = FakeEnvironment()
        _result, config, stage_dir = await self._run(environment)

        with open(self.master, "rb") as fh:
            self.assertEqual(fh.read(), ROTATED_AUTH)
        self.assertFalse(os.path.exists(stage_dir))
        self.assertEqual(
            environment.downloads[0][0], "/tmp/codex-secrets/auth.json"
        )
        capture_index = environment.events.index(
            ("download", "/tmp/codex-secrets/auth.json")
        )
        cleanup_index = next(
            i for i, event in enumerate(environment.events)
            if event[0] == "exec" and "rm -rf" in event[1]
        )
        self.assertLess(capture_index, cleanup_index)
        self.assertEqual(environment.remote_files, {})
        self.assertEqual(stat.S_IMODE(os.stat(self.master).st_mode), 0o600)

        exposed = json.dumps({
            "config": config.agent_extra_env(),
            "commands": environment.commands,
            "uploads": environment.uploads,
            "downloads": environment.downloads,
            "logs": environment.logs,
            "artifacts": environment.artifacts,
        })
        for secret in ("old-r", "new-r", '"access_token":"old"', '"access_token":"new"'):
            self.assertNotIn(secret, exposed)

    async def test_agent_failure_still_persists_rotation_and_cleans(self):
        environment = FakeEnvironment(agent_failure=True)
        with self.assertRaisesRegex(RuntimeError, "synthetic agent failure"):
            await self._run(environment)
        with open(self.master, "rb") as fh:
            self.assertEqual(fh.read(), ROTATED_AUTH)
        self.assertEqual(environment.remote_files, {})

    async def test_capture_failure_is_fail_closed_but_cleanup_still_runs(self):
        environment = FakeEnvironment(download_failure=True)
        with self.assertRaises(harbor_oauth.HarborOAuthCaptureError):
            await self._run(environment)
        with open(self.master, "rb") as fh:
            self.assertEqual(fh.read(), OLD_AUTH)
        self.assertEqual(environment.remote_files, {})
        self.assertTrue(any(
            "rm -rf" in entry["command"] for entry in environment.commands
        ))

    async def test_changed_upstream_cleanup_boundary_is_explicitly_unsupported(self):
        environment = FakeEnvironment()
        with self.assertRaises(harbor_oauth.HarborOAuthUnsupportedError):
            await self._run(environment, base=ChangedCleanupHarborCodex)
        with open(self.master, "rb") as fh:
            self.assertEqual(fh.read(), OLD_AUTH)

    async def test_agent_class_rejects_missing_lifecycle_members(self):
        class IncompatibleCodex:
            pass

        with self.assertRaisesRegex(
            harbor_oauth.HarborOAuthUnsupportedError, "lifecycle members"
        ):
            harbor_codex_agent._build_agent_class(IncompatibleCodex)


class OptionalDependencyTests(unittest.TestCase):
    def test_agent_module_import_does_not_import_harbor(self):
        imported = []
        real_import = __import__

        def tracking_import(name, *args, **kwargs):
            if name == "harbor" or name.startswith("harbor."):
                imported.append(name)
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=tracking_import):
            importlib.reload(harbor_codex_agent)
        self.assertEqual(imported, [])

    def test_loading_agent_without_harbor_has_clear_setup_error(self):
        blocked = {
            name: module for name, module in sys.modules.items()
            if name == "harbor" or name.startswith("harbor.")
        }
        for name in blocked:
            sys.modules.pop(name, None)
        try:
            with mock.patch.dict(sys.modules, {"harbor": None}):
                with self.assertRaisesRegex(
                    harbor_oauth.HarborOAuthSetupError,
                    "optional 'harbor' package",
                ):
                    harbor_codex_agent.load_agent_class()
        finally:
            sys.modules.update(blocked)

    def test_lazy_loader_subclasses_codex_from_fake_harbor_module(self):
        modules = {}
        for name in ("harbor", "harbor.agents", "harbor.agents.installed"):
            modules[name] = ModuleType(name)
        codex_module = ModuleType("harbor.agents.installed.codex")
        codex_module.Codex = FakeHarborCodex
        modules[codex_module.__name__] = codex_module

        with mock.patch.dict(sys.modules, modules):
            agent_class = harbor_codex_agent.load_agent_class()
        self.assertTrue(issubclass(agent_class, FakeHarborCodex))


if __name__ == "__main__":
    unittest.main()
