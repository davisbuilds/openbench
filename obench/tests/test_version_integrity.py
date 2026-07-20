#!/usr/bin/env python3
"""Tests for Docker image CLI pin maintenance tooling."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest import mock


from obench import bump_clis  # noqa: E402


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


class TestCheckUpstream(unittest.TestCase):
    def _run(self, latest, *, failing_package=None):
        dockerfile_text = TestBumpClisCheck()._fixture_dockerfile()
        calls = []

        def fake_urlopen(request, timeout):
            self.assertEqual(request.full_url.split("/", 3)[:3],
                             ["https:", "", "registry.npmjs.org"])
            self.assertEqual(timeout, 10)
            package = urllib.parse.unquote(request.full_url.rsplit("/", 1)[-1])
            calls.append(package)
            if package == failing_package:
                raise OSError("registry unavailable")
            return io.BytesIO(json.dumps({"dist-tags": {"latest": latest[package]}}).encode())

        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write(dockerfile_text)
            dockerfile = fh.name
        try:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.object(bump_clis.urllib.request, "urlopen", side_effect=fake_urlopen), \
                    mock.patch.object(bump_clis, "run_cmd", side_effect=AssertionError("must not install")), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = bump_clis.main(["--check-upstream", "--dockerfile", dockerfile])
        finally:
            os.unlink(dockerfile)
        return code, stdout.getvalue(), stderr.getvalue(), calls

    def test_behind_pin_exits_nonzero_and_prints_pin_vs_latest(self):
        latest = {
            "@openai/codex": "0.144.2",
            "@earendil-works/pi-coding-agent": "0.80.6",
            "@anthropic-ai/claude-code": "2.1.206",
            "@xai-official/grok": "0.2.93",
            "opencode-ai": "1.17.18",
        }
        code, stdout, stderr, calls = self._run(latest)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 5)
        self.assertIn("pin", stdout.splitlines()[0])
        self.assertIn("0.144.1", stdout)
        self.assertIn("0.144.2", stdout)
        self.assertIn("behind", stdout)
        self.assertIn("cursor", stdout)
        self.assertIn("manual", stdout)
        self.assertEqual(stderr, "")

    def test_current_pins_exit_zero(self):
        latest = {
            "@openai/codex": "0.144.1",
            "@earendil-works/pi-coding-agent": "0.80.6",
            "@anthropic-ai/claude-code": "2.1.206",
            "@xai-official/grok": "0.2.93",
            "opencode-ai": "1.17.18",
        }
        code, stdout, stderr, _calls = self._run(latest)
        self.assertEqual(code, 0)
        self.assertNotIn("behind", stdout)
        self.assertEqual(stdout.count("current"), 5)
        self.assertEqual(stderr, "")

    def test_registry_error_warns_and_skips_without_failing_check(self):
        latest = {
            "@openai/codex": "0.144.1",
            "@earendil-works/pi-coding-agent": "0.80.6",
            "@anthropic-ai/claude-code": "2.1.206",
            "@xai-official/grok": "0.2.93",
            "opencode-ai": "1.17.18",
        }
        code, stdout, stderr, calls = self._run(
            latest, failing_package="@anthropic-ai/claude-code")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 5)
        self.assertIn("claude", stdout)
        self.assertIn("unknown", stdout)
        self.assertIn("skipped", stdout)
        self.assertIn("WARN: claude: npm registry lookup failed; skipped", stderr)


class TestHostSync(unittest.TestCase):
    def test_sync_host_installs_only_drifted_npm_pins_and_never_uses_docker(self):
        dockerfile_text = TestBumpClisCheck()._fixture_dockerfile()
        versions = {
            "codex": "0.144.0",
            "pi": "0.80.6",
            "claude": "2.1.206",
            "grok": "0.2.93",
            "opencode": "1.17.18",
            "cursor-agent": "2026.07.08-old",
        }
        calls = []

        def fake_run_cmd(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ["npm", "install", "-g"]:
                self.assertEqual(cmd[3], "@openai/codex@0.144.1")
                versions["codex"] = "0.144.1"
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            self.assertEqual(cmd[1:], ["--version"])
            version = versions[cmd[0]]
            return SimpleNamespace(returncode=0, stdout=f"{cmd[0]} {version}\n", stderr="")

        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write(dockerfile_text)
            dockerfile = fh.name
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                bump_clis.sync_host(dockerfile, command_runner=fake_run_cmd)
        finally:
            os.unlink(dockerfile)

        installs = [cmd for cmd in calls if cmd[:3] == ["npm", "install", "-g"]]
        self.assertEqual(installs, [["npm", "install", "-g", "@openai/codex@0.144.1"]])
        self.assertFalse(any(cmd[0] == "docker" for cmd in calls))
        self.assertIn("codex: before=0.144.0 pin=0.144.1", output.getvalue())
        self.assertIn("codex: after=0.144.1 pin=0.144.1", output.getvalue())
        self.assertIn("cursor: manual sync required", output.getvalue())
        self.assertIn("cursor: after=2026.07.08-old pin=2026.07.09-a3815c0", output.getvalue())

    def test_reported_version_does_not_accept_prefix_matches(self):
        self.assertEqual(bump_clis.reported_version("grok v0.2.93 (hash)"), "0.2.93")
        self.assertNotEqual(bump_clis.reported_version("grok 0.2.93"), "0.2.9")

    def test_missing_cli_is_treated_as_unavailable_for_sync(self):
        def missing(_cmd, **_kwargs):
            raise FileNotFoundError("missing")

        version, raw = bump_clis.host_cli_version(
            bump_clis.PIN_BY_KEY["codex"], command_runner=missing)
        self.assertIsNone(version)
        self.assertIn("missing", raw)


class TestImagePinLabels(unittest.TestCase):
    def test_explicit_empty_key_selection_checks_nothing(self):
        mismatches = bump_clis.image_pin_mismatches(
            {"codex": "0.144.1"}, {}, keys=[])
        self.assertEqual(mismatches, [])


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
