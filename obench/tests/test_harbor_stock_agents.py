"""Offline proof for Harbor Cursor and Devin subscription profiles."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest import mock

from obench.atif import validate_trajectory
from obench.harbor_agents import _subscription
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


if __name__ == "__main__":
    unittest.main()
