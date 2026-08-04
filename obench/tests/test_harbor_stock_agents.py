"""Offline proof for Harbor Cursor and Devin subscription profiles."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from pathlib import PurePosixPath
import stat
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from obench.atif import validate_trajectory
from obench.harbor_agents import _subscription
from obench.harbor_agents import cursor as cursor_agent
from obench.harbor_agents import devin as devin_agent
from obench.harbor_agents.cursor import convert_cursor_stream
from obench.harbor_agents.devin import normalize_devin_export
from obench.harbor_oauth import (
    HarborOAuthSetupError,
    HarborOAuthUnsupportedError,
)


class HarborStockAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_cursor_auth_archive_keeps_only_auth_state(self):
        cli_config = self.root / ".cursor" / "cli-config.json"
        cli_config.parent.mkdir()
        cli_config.write_text(
            json.dumps(
                {
                    "authInfo": {"accessToken": "secret"},
                    "model": "must-not-leak",
                    "permissions": {"shell": "deny"},
                }
            ),
            encoding="utf-8",
        )
        with _subscription.staged_subscription_auth(
            "cursor", (str(cli_config),)
        ) as archive:
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(archive.parent.stat().st_mode), 0o700
            )
            with tarfile.open(archive, "r:gz") as handle:
                names = [
                    member.name
                    for member in handle.getmembers()
                    if member.isfile()
                ]
                self.assertEqual(names, [".cursor/cli-config.json"])
                payload = json.load(handle.extractfile(names[0]))
            self.assertEqual(payload, {"authInfo": {"accessToken": "secret"}})

    def test_devin_archive_copies_whitelist_and_rejects_symlinks(self):
        credentials = self.root / ".local" / "share" / "devin"
        credentials.mkdir(parents=True)
        credential_file = credentials / "credentials.toml"
        credential_file.write_text('token = "secret"\n', encoding="utf-8")
        adjacent = self.root / ".agents" / "skills"
        adjacent.mkdir(parents=True)
        (adjacent / "behavior.md").write_text("do not stage", encoding="utf-8")

        with (
            mock.patch.object(_subscription.Path, "home", return_value=self.root),
            _subscription.staged_subscription_auth(
                "devin", (str(credentials),)
            ) as archive,
        ):
            with tarfile.open(archive, "r:gz") as handle:
                names = {
                    member.name
                    for member in handle.getmembers()
                    if member.isfile()
                }
            self.assertEqual(
                names, {".local/share/devin/credentials.toml"}
            )

        link = credentials / "linked"
        link.symlink_to(credential_file)
        with (
            mock.patch.object(_subscription.Path, "home", return_value=self.root),
            self.assertRaisesRegex(HarborOAuthSetupError, "symlink"),
            _subscription.staged_subscription_auth(
                "devin", (str(credentials),)
            ),
        ):
            pass

    def test_cursor_stream_converts_documented_events_to_valid_atif(self):
        source = self.root / "cursor.jsonl"
        events = [
            {
                "type": "system",
                "subtype": "init",
                "session_id": "session-1",
                "model": "GPT",
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Fix it"}],
                },
                "session_id": "session-1",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Reading."}],
                },
                "session_id": "session-1",
            },
            {
                "type": "tool_call",
                "subtype": "started",
                "call_id": "call-1",
                "tool_call": {
                    "readToolCall": {"args": {"path": "README.md"}}
                },
                "session_id": "session-1",
            },
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": "call-1",
                "tool_call": {
                    "readToolCall": {
                        "args": {"path": "README.md"},
                        "result": {"success": {"totalLines": 4}},
                    }
                },
                "session_id": "session-1",
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Done",
                "session_id": "session-1",
                "usage": {
                    "inputTokens": 12,
                    "outputTokens": 4,
                    "cacheReadTokens": 3,
                },
            },
        ]
        source.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )

        trajectory = convert_cursor_stream(
            source,
            version="2026.07.09-a3815c0",
            model_name="gpt-5.6-sol",
        )

        self.assertEqual(validate_trajectory(trajectory), [])
        self.assertEqual(trajectory["agent"]["name"], "cursor")
        self.assertEqual(trajectory["session_id"], "session-1")
        tool_step = trajectory["steps"][2]
        self.assertEqual(
            tool_step["tool_calls"][0]["function_name"], "read"
        )
        self.assertEqual(
            tool_step["observation"]["results"][0]["source_call_id"],
            "call-1",
        )

    def test_cursor_stream_fails_without_success_or_agent_evidence(self):
        source = self.root / "cursor.jsonl"
        source.write_text(
            '{"type":"result","subtype":"error","is_error":true}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            HarborOAuthUnsupportedError, "successful terminal"
        ):
            convert_cursor_stream(
                source,
                version="2026.07.09-a3815c0",
                model_name="gpt-5.6-sol",
            )

        source.write_text(
            '{"type":"result","subtype":"success","is_error":false}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            HarborOAuthUnsupportedError, "no attributable agent"
        ):
            convert_cursor_stream(
                source,
                version="2026.07.09-a3815c0",
                model_name="gpt-5.6-sol",
            )

    def test_devin_export_is_validated_without_synthesizing_steps(self):
        source = self.root / "devin-export.json"
        original_steps = [
            {"step_id": 1, "source": "user", "message": "Fix it"},
            {
                "step_id": 2,
                "source": "agent",
                "message": "Done",
                "model_name": "gpt-5-6-sol-medium",
            },
        ]
        source.write_text(
            json.dumps(
                {
                    "schema_version": "ATIF-v1.6",
                    "agent": {"name": "old", "version": "old"},
                    "steps": original_steps,
                    "final_metrics": {"total_steps": 2},
                }
            ),
            encoding="utf-8",
        )

        trajectory = normalize_devin_export(
            source,
            version="3000.2.17",
            model_name="gpt-5.6-sol",
        )

        self.assertEqual(validate_trajectory(trajectory), [])
        self.assertEqual(trajectory["steps"], original_steps)
        self.assertEqual(
            trajectory["agent"],
            {
                "name": "devin",
                "version": "3000.2.17",
                "model_name": "gpt-5.6-sol",
            },
        )

    def test_devin_export_fails_closed_when_source_steps_are_absent(self):
        source = self.root / "devin-export.json"
        source.write_text('{"steps":[]}\n', encoding="utf-8")
        with self.assertRaisesRegex(
            HarborOAuthUnsupportedError, "no source steps"
        ):
            normalize_devin_export(
                source,
                version="3000.2.17",
                model_name="gpt-5.6-sol",
            )

    def test_custom_agents_use_canonical_native_commands_and_pins(self):
        class FakeBase:
            def __init__(
                self,
                *,
                logs_dir,
                model_name,
                version,
                extra_env,
                **kwargs,
            ):
                del kwargs
                self.logs_dir = Path(logs_dir)
                self.model_name = model_name
                self._version = version
                self._extra_env = dict(extra_env)
                self.commands = []

            def _get_env(self, key):
                return self._extra_env.get(key)

            def version(self):
                return self._version

            def render_instruction(self, instruction):
                return instruction

            async def ensure_system_dependencies(self, environment, dependencies):
                del environment, dependencies

            async def exec_as_agent(self, environment, command, **kwargs):
                del environment
                self.commands.append(("agent", command, kwargs))

            async def exec_as_root(self, environment, command, **kwargs):
                del environment
                self.commands.append(("root", command, kwargs))

        class FakeEnvironment:
            default_user = None

            def __init__(self):
                self.uploads = []

            async def upload_file(self, source, destination):
                self.uploads.append((Path(source), destination))

        paths = SimpleNamespace(app_dir=PurePosixPath("/app"))
        cases = (
            (
                cursor_agent,
                "OpenBenchCursorSubscription",
                "cursor",
                "2026.07.09-a3815c0",
                "gpt-5.6-terra",
                _subscription.CURSOR_AUTH_ARCHIVE_ENV,
                "cursor-agent -p --force --trust "
                "--model gpt-5.6-terra-medium "
                "--output-format stream-json --workspace /app",
                "downloads.cursor.com/lab/2026.07.09-a3815c0/",
            ),
            (
                devin_agent,
                "OpenBenchDevinSubscription",
                "devin",
                "3000.2.17",
                "gpt-5.6-sol",
                _subscription.DEVIN_AUTH_ARCHIVE_ENV,
                "devin -p --permission-mode dangerous "
                "--model gpt-5-6-sol-medium "
                "--export /logs/agent/devin-export.json --",
                _DEVIN_INSTALL_PROOF,
            ),
        )
        for (
            module,
            class_name,
            harness,
            version,
            model,
            env_name,
            run_fragment,
            install_fragment,
        ) in cases:
            with self.subTest(harness=harness):
                archive = self.root / f"{harness}.tar.gz"
                archive.write_bytes(b"private")
                archive.chmod(0o600)
                self.root.chmod(0o700)
                agent_class = module._build_agent_class(FakeBase, paths)
                self.assertEqual(agent_class.__name__, class_name)
                agent = agent_class(
                    logs_dir=self.root / f"{harness}-logs",
                    model_name=model,
                    version=version,
                    extra_env={env_name: str(archive)},
                )
                environment = FakeEnvironment()
                asyncio.run(agent.install(environment))
                asyncio.run(agent.run("Fix it", environment, object()))

                install_command = next(
                    command
                    for role, command, _ in agent.commands
                    if role == "root"
                )
                run_command = next(
                    command
                    for role, command, _ in agent.commands
                    if role == "agent" and run_fragment in command
                )
                self.assertIn(install_fragment, install_command)
                self.assertIn(run_fragment, run_command)
                self.assertNotIn("API_KEY=", run_command)
                self.assertEqual(len(environment.uploads), 1)


_DEVIN_INSTALL_PROOF = (
    "f0e1e9363afc6ee68c4ef87bab4aeb7ff5cc08a5fa838350ef3ceefdbb2a2be2"
)


if __name__ == "__main__":
    unittest.main()
