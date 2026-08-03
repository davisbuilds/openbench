"""Focused OAuth lifecycle tests for OpenBench Harbor profile agents."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest

from obench import auth_persist
from obench.atif import validate_trajectory
from obench.harbor_agents import codex_profile
from obench.harbor_agents import opencode as opencode_agent
from obench.harbor_agents import pi as pi_agent
from obench.harbor_oauth import HarborOAuthUnsupportedError


AUTH_0 = b'{"account":"owner","access_token":"zero"}'
AUTH_1 = b'{"account":"owner","access_token":"one"}'
AUTH_2 = b'{"account":"owner","access_token":"two"}'


class NullLogger:
    def exception(self, *_args, **_kwargs):
        pass


class FakeEnvironment:
    default_user = "agent"

    def __init__(self, rotated_auth):
        self.rotated_auth = rotated_auth
        self.remote_files = {}
        self.commands = []
        self.uploaded_bytes = []
        self.events = []

    async def upload_file(self, source_path, target_path):
        content = Path(source_path).read_bytes()
        self.uploaded_bytes.append(content)
        self.remote_files[target_path] = content
        self.events.append(("upload", target_path))

    async def download_file(self, source_path, target_path):
        Path(target_path).write_bytes(self.remote_files[source_path])
        self.events.append(("download", source_path))

    async def exec(self, command, **_kwargs):
        self.commands.append(command)
        self.events.append(("exec", command))
        if "pi --print" in command:
            self.remote_files[
                "/tmp/openbench-pi-home/.pi/agent/auth.json"
            ] = self.rotated_auth
        if "opencode --model=" in command:
            self.remote_files[
                (
                    "/tmp/openbench-opencode-home/.local/share/"
                    "opencode/auth.json"
                )
            ] = self.rotated_auth
        if command.startswith("rm -rf /tmp/openbench-"):
            prefix = command.split()[2]
            for path in list(self.remote_files):
                if path.startswith(prefix):
                    del self.remote_files[path]
        return SimpleNamespace(return_code=0, stdout="", stderr="")


class FakeInstalledBase:
    _OUTPUT_FILENAME = ""
    _DEFAULT_CONFIG = {}
    SUPPORTS_ATIF = False
    SUPPORTS_RESUME = True

    def __init__(
        self,
        *,
        logs_dir,
        model_name,
        extra_env,
        version,
        thinking=None,
        variant=None,
        opencode_config=None,
        **_kwargs,
    ):
        self.logs_dir = Path(logs_dir)
        self.model_name = model_name
        self._extra_env = dict(extra_env)
        self._version = version
        self._resume = False
        self.skills_dir = None
        self.mcp_servers = []
        self.logger = NullLogger()
        self._thinking = thinking
        self._variant = variant
        self._opencode_config = opencode_config or {}
        self._instruction = None

    def _get_env(self, key):
        return self._extra_env.get(key)

    def version(self):
        return self._version

    def render_instruction(self, instruction):
        return instruction

    async def exec_as_agent(self, environment, command, **kwargs):
        return await environment.exec(command, **kwargs)

    async def exec_as_root(self, environment, command, **kwargs):
        return await environment.exec(command, **kwargs)

    def _build_register_skills_command(self):
        return None

    @staticmethod
    def _deep_merge(base, override):
        for key, value in override.items():
            if (
                key in base
                and isinstance(base[key], dict)
                and isinstance(value, dict)
            ):
                FakeInstalledBase._deep_merge(base[key], value)
            else:
                base[key] = value
        return base

    def _error_messages(self):
        return []


class FakePi(FakeInstalledBase):
    _OUTPUT_FILENAME = "pi.txt"

    def build_cli_flags(self):
        return f"--thinking {self._thinking}" if self._thinking else ""


class FakeOpenCode(FakeInstalledBase):
    _OUTPUT_FILENAME = "opencode.txt"
    SUPPORTS_ATIF = True

    def build_cli_flags(self):
        return f"--variant {self._variant}" if self._variant else ""

    def populate_context_post_run(self, context):
        context.used_builtin_opencode_atif = True


def _write_private(path, content):
    Path(path).write_bytes(content)
    os.chmod(path, 0o600)


class AgentLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.chmod(self.root, 0o700)
        self.input_path = self.root / "auth.json"
        self.return_path = self.root / "auth-return.json"
        _write_private(self.input_path, AUTH_0)

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self, harness, proxy=None):
        if harness == "pi":
            env = {
                pi_agent.PI_AUTH_JSON_PATH: str(self.input_path),
                pi_agent.PI_AUTH_RETURN_PATH: str(self.return_path),
            }
            if proxy:
                env[pi_agent.PI_BASE_URL] = proxy
            return env
        return {
            opencode_agent.OPENCODE_AUTH_JSON_PATH: str(self.input_path),
            opencode_agent.OPENCODE_AUTH_RETURN_PATH: str(self.return_path),
        }

    async def test_pi_two_trials_refresh_input_and_preserve_final_return(self):
        agent_class = pi_agent._build_agent_class(FakePi)
        first_env = FakeEnvironment(AUTH_1)
        first = agent_class(
            logs_dir=self.root,
            model_name="openai-codex/gpt-5.6-sol",
            extra_env=self._env(
                "pi", "http://proxy/cell/token/codex/backend-api"
            ),
            version="0.80.10",
            thinking="medium",
        )
        await first.run("solve", first_env, SimpleNamespace())

        self.assertEqual(self.input_path.read_bytes(), AUTH_1)
        self.assertEqual(self.return_path.read_bytes(), AUTH_1)
        self.assertEqual(first_env.uploaded_bytes, [AUTH_0])
        self.assertTrue(
            any(
                '"openai-codex": {"baseUrl": '
                '"http://proxy/cell/token/codex/backend-api"}' in command
                for command in first_env.commands
            )
        )

        second_env = FakeEnvironment(AUTH_2)
        second = agent_class(
            logs_dir=self.root,
            model_name="openai-codex/gpt-5.6-sol",
            extra_env=self._env("pi"),
            version="0.80.10",
            thinking="medium",
        )
        await second.run("solve again", second_env, SimpleNamespace())

        self.assertEqual(second_env.uploaded_bytes, [AUTH_1])
        self.assertEqual(self.input_path.read_bytes(), AUTH_2)
        self.assertEqual(self.return_path.read_bytes(), AUTH_2)
        self.assertEqual(stat.S_IMODE(self.return_path.stat().st_mode), 0o600)
        self.assertLess(
            next(i for i, event in enumerate(second_env.events) if event[0] == "download"),
            max(
                i
                for i, event in enumerate(second_env.events)
                if event[0] == "exec" and event[1] == "rm -rf /tmp/openbench-pi-home"
            ),
        )

    async def test_opencode_two_trials_refresh_input_and_retain_builtin_atif(self):
        agent_class = opencode_agent._build_agent_class(FakeOpenCode)
        self.assertTrue(agent_class.SUPPORTS_ATIF)
        self.assertFalse(agent_class.SUPPORTS_RESUME)
        for expected_input, rotation in ((AUTH_0, AUTH_1), (AUTH_1, AUTH_2)):
            environment = FakeEnvironment(rotation)
            agent = agent_class(
                logs_dir=self.root,
                model_name="openai/gpt-5.6-sol",
                extra_env=self._env("opencode"),
                version="1.18.3",
                variant="medium",
            )
            await agent.run("solve", environment, SimpleNamespace())
            self.assertEqual(environment.uploaded_bytes, [expected_input])
            self.assertTrue(
                any(
                    "unset OPENAI_API_KEY OPENAI_BASE_URL" in command
                    and "--variant medium" in command
                    and "--dangerously-skip-permissions" in command
                    for command in environment.commands
                )
            )
            context = SimpleNamespace()
            agent.populate_context_post_run(context)
            self.assertTrue(context.used_builtin_opencode_atif)

        self.assertEqual(self.input_path.read_bytes(), AUTH_2)
        self.assertEqual(self.return_path.read_bytes(), AUTH_2)

    async def test_wrappers_reject_non_oauth_provider_without_upload(self):
        pi_class = pi_agent._build_agent_class(FakePi)
        pi = pi_class(
            logs_dir=self.root,
            model_name="openai/gpt-5.6-sol",
            extra_env=self._env("pi"),
            version="0.80.10",
            thinking="medium",
        )
        environment = FakeEnvironment(AUTH_1)
        with self.assertRaisesRegex(
            HarborOAuthUnsupportedError, "openai-codex"
        ):
            await pi.run("solve", environment, SimpleNamespace())
        self.assertEqual(environment.uploaded_bytes, [])

        opencode_class = opencode_agent._build_agent_class(FakeOpenCode)
        opencode = opencode_class(
            logs_dir=self.root,
            model_name="anthropic/model",
            extra_env=self._env("opencode"),
            version="1.18.3",
            variant="medium",
        )
        with self.assertRaisesRegex(HarborOAuthUnsupportedError, "openai"):
            await opencode.run("solve", environment, SimpleNamespace())
        self.assertEqual(environment.uploaded_bytes, [])

    async def test_real_host_lease_persists_final_sequential_rotation(self):
        master = self.root / "master-auth.json"
        _write_private(master, AUTH_0)
        agent_class = pi_agent._build_agent_class(FakePi)

        with auth_persist.auth_file_lease(master) as lease:
            lease.stage(self.input_path)
            for rotation in (AUTH_1, AUTH_2):
                agent = agent_class(
                    logs_dir=self.root,
                    model_name="openai-codex/gpt-5.6-sol",
                    extra_env=self._env("pi"),
                    version="0.80.10",
                    thinking="medium",
                )
                await agent.run(
                    "solve", FakeEnvironment(rotation), SimpleNamespace()
                )
            self.assertTrue(lease.persist(self.return_path))

        self.assertEqual(master.read_bytes(), AUTH_2)
        self.assertEqual(self.input_path.read_bytes(), AUTH_2)
        self.assertEqual(self.return_path.read_bytes(), AUTH_2)


class PiAtifTests(unittest.TestCase):
    def test_pi_populate_context_writes_valid_atif_and_aggregate_usage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            usage = {
                "input": 10,
                "output": 5,
                "cacheRead": 2,
                "cacheWrite": 1,
                "reasoning": 3,
                "totalTokens": 18,
                "cost": {"total": 0.25},
            }
            events = [
                {"type": "session", "version": 3, "id": "session-1", "cwd": "/app"},
                {
                    "type": "turn_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                        "usage": usage,
                    },
                },
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "solve"}],
                        },
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "done"}],
                            "model": "gpt-5.6-sol",
                            "usage": usage,
                        },
                    ],
                },
            ]
            (root / "pi.txt").write_text(
                "\n".join(json.dumps(event) for event in events) + "\n"
            )
            agent_class = pi_agent._build_agent_class(FakePi)
            agent = agent_class(
                logs_dir=root,
                model_name="openai-codex/gpt-5.6-sol",
                extra_env={},
                version="0.80.10",
                thinking="medium",
            )
            context = SimpleNamespace()

            agent.populate_context_post_run(context)

            trajectory = json.loads((root / "trajectory.json").read_text())
            self.assertEqual(validate_trajectory(trajectory), [])
            self.assertEqual(trajectory["agent"]["name"], "pi")
            self.assertEqual(trajectory["agent"]["version"], "0.80.10")
            self.assertEqual(
                trajectory["agent"]["model_name"],
                "openai-codex/gpt-5.6-sol",
            )
            self.assertEqual(context.n_input_tokens, 13)
            self.assertEqual(context.n_output_tokens, 5)
            self.assertEqual(context.n_cache_tokens, 2)
            self.assertEqual(context.cost_usd, 0.25)


class CodexProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_followup_refreshes_staged_input_and_keeps_return(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "auth.json"
            return_path = root / "auth-return.json"
            _write_private(input_path, AUTH_0)

            class FakeCodexOAuth:
                def __init__(self, *, extra_env):
                    self._extra_env = extra_env

                def _get_env(self, key):
                    return self._extra_env.get(key)

                async def run(self, instruction, environment, context):
                    del instruction, environment, context
                    _write_private(return_path, AUTH_1)
                    return "ok"

            agent_class = codex_profile._build_agent_class(FakeCodexOAuth)
            agent = agent_class(
                extra_env={
                    "CODEX_AUTH_JSON_PATH": str(input_path),
                    "OPENBENCH_CODEX_AUTH_RETURN_PATH": str(return_path),
                }
            )
            result = await agent.run("solve", None, None)

            self.assertEqual(result, "ok")
            self.assertEqual(input_path.read_bytes(), AUTH_1)
            self.assertEqual(return_path.read_bytes(), AUTH_1)


if __name__ == "__main__":
    unittest.main()
