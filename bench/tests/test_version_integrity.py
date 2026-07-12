#!/usr/bin/env python3
"""Tests for Docker image CLI pin maintenance tooling."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BENCH_DIR)

import bump_clis  # noqa: E402


class TestBumpClisCheck(unittest.TestCase):
    def _fixture_dockerfile(self):
        return "\n".join([
            "FROM node:22-slim",
            "ARG CODEX_VERSION=0.144.1",
            "ARG PI_VERSION=0.80.6",
            "ARG CLAUDE_VERSION=2.1.206",
            "ARG GROK_VERSION=0.2.93",
            "ARG OPENCODE_VERSION=1.17.18",
            "ARG CURSOR_AGENT_VERSION=2026.07.09-a3815c0",
            "",
        ])

    def test_check_prints_current_vs_latest_table_from_mocked_npm_view(self):
        latest = {
            "@openai/codex": "0.144.2",
            "@earendil-works/pi-coding-agent": "0.80.6",
            "@anthropic-ai/claude-code": "2.1.207",
            "@xai-official/grok": "0.2.93",
            "opencode-ai": "1.17.19",
        }
        calls = []

        def fake_run_cmd(cmd, **kwargs):
            calls.append(cmd)
            self.assertEqual(cmd[:2], ["npm", "view"])
            return SimpleNamespace(returncode=0, stdout=latest[cmd[2]] + "\n", stderr="")

        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write(self._fixture_dockerfile())
            dockerfile = fh.name
        try:
            with mock.patch.object(bump_clis, "run_cmd", side_effect=fake_run_cmd):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = bump_clis.main(["--check", "--dockerfile", dockerfile])
            out = buf.getvalue()
        finally:
            os.unlink(dockerfile)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 5)
        self.assertIn("@openai/codex", out)
        self.assertIn("0.144.1", out)
        self.assertIn("0.144.2", out)
        self.assertIn("update", out)
        self.assertIn("(installer)", out)  # cursor is installer-pinned, not npm-viewed

    def test_check_json_emits_parseable_rows(self):
        latest = {pin.package: "9.9.9" for pin in bump_clis.PINS if pin.package}

        def fake_run_cmd(cmd, **kwargs):
            return SimpleNamespace(returncode=0, stdout=latest[cmd[2]] + "\n", stderr="")

        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write(self._fixture_dockerfile())
            dockerfile = fh.name
        try:
            with mock.patch.object(bump_clis, "run_cmd", side_effect=fake_run_cmd):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = bump_clis.main(["--check", "--json", "--dockerfile", dockerfile])
            rows = json.loads(buf.getvalue())
        finally:
            os.unlink(dockerfile)
        self.assertEqual(code, 0)
        self.assertEqual({row["key"] for row in rows}, {pin.key for pin in bump_clis.PINS})
        self.assertEqual(next(row for row in rows if row["key"] == "cursor")["latest"], "n/a")


class TestDockerfilePinRewrite(unittest.TestCase):
    def test_rewrite_dockerfile_pins_updates_only_selected_args(self):
        original = "\n".join([
            "FROM node:22-slim",
            "ARG CODEX_VERSION=0.144.1",
            "ARG PI_VERSION=0.80.6",
            "ARG CURSOR_AGENT_VERSION=2026.07.09-a3815c0",
            "RUN npm install -g @openai/codex@${CODEX_VERSION}",
            "",
        ])
        rewritten = bump_clis.rewrite_dockerfile_pins(original, {
            "@openai/codex": "0.145.0",
            "cursor": "2026.07.10-next",
        })
        self.assertIn("ARG CODEX_VERSION=0.145.0", rewritten)
        self.assertIn("ARG PI_VERSION=0.80.6", rewritten)
        self.assertIn("ARG CURSOR_AGENT_VERSION=2026.07.10-next", rewritten)
        self.assertIn("@openai/codex@${CODEX_VERSION}", rewritten)


if __name__ == "__main__":
    unittest.main()
