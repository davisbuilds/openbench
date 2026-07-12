#!/usr/bin/env python3
"""Unit tests for the claude-opus-4-8 frontier routing entries.

No live model calls: subprocess.run is stubbed, auth/key checks are forced with
temporary files or env vars, and the codex bridge reachability probe is patched.
"""

import importlib.util
import json
import os
import tempfile
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTERS_DIR = os.path.join(BENCH_DIR, "adapters")


def load(name):
    spec = importlib.util.spec_from_file_location(
        f"frontier_{name}", os.path.join(ADAPTERS_DIR, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class EnvPatch:
    def __enter__(self):
        self.saved = dict(os.environ)
        return os.environ

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self.saved)


class TestPiOpus(unittest.TestCase):
    def setUp(self):
        self.pi = load("pi")

    def test_missing_pi_auth_is_setup_needed(self):
        old = self.pi._REAL_AUTH
        self.pi._REAL_AUTH = "/definitely/missing/pi/auth.json"
        try:
            res = self.pi.run("hi", "/tmp", "claude-opus-4-8", 5)
        finally:
            self.pi._REAL_AUTH = old
        self.assertFalse(res["completed"])
        self.assertIn("SETUP-NEEDED", res["error"])
        self.assertIsNone(res["cmd"])

    def test_pi_auth_without_anthropic_provider_is_setup_needed(self):
        old = self.pi._REAL_AUTH
        fd, auth = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            json.dump({"openai-codex": {"type": "oauth", "access": "x"}}, fh)
        self.pi._REAL_AUTH = auth
        try:
            res = self.pi.run("hi", "/tmp", "claude-opus-4-8", 5)
        finally:
            self.pi._REAL_AUTH = old
            os.unlink(auth)
        self.assertFalse(res["completed"])
        self.assertIn("SETUP-NEEDED", res["error"])
        self.assertIn("anthropic", res["error"])
        self.assertIsNone(res["cmd"])

    def test_constructs_gpt56_openai_codex_medium_commands(self):
        variants = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
        old_run = self.pi.subprocess.run
        old_auth = self.pi._REAL_AUTH
        fd, auth = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            json.dump({"openai-codex": {"type": "oauth", "access": "x"}}, fh)

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            usage = {"input": 3, "cacheRead": 0, "cacheWrite": 0, "output": 4, "reasoning": 1, "totalTokens": 7}
            return FakeProc(stdout=json.dumps({"type": "turn_end", "message": {"usage": usage}}) + "\n")

        self.pi.subprocess.run = fake_run
        self.pi._REAL_AUTH = auth
        try:
            for model in variants:
                with self.subTest(model=model):
                    res = self.pi.run("hi", "/tmp", model, 5)
                    self.assertTrue(res["completed"])
                    cmd = calls[-1][0]
                    self.assertEqual(cmd[cmd.index("--provider") + 1], "openai-codex")
                    self.assertEqual(cmd[cmd.index("--model") + 1], model)
                    self.assertEqual(cmd[cmd.index("--thinking") + 1], "medium")
                    self.assertEqual(res["token_basis"], "vendor_split")
        finally:
            self.pi.subprocess.run = old_run
            self.pi._REAL_AUTH = old_auth
            os.unlink(auth)

    def test_constructs_anthropic_medium_command(self):
        calls = []
        old_run = self.pi.subprocess.run
        old_auth = self.pi._REAL_AUTH
        fd, auth = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            json.dump({"anthropic": {"type": "oauth", "access": "x"}}, fh)

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeProc(stdout=json.dumps({"type": "agent_end", "messages": []}) + "\n")

        self.pi.subprocess.run = fake_run
        self.pi._REAL_AUTH = auth
        try:
            res = self.pi.run("hi", "/tmp", "claude-opus-4-8", 5)
        finally:
            self.pi.subprocess.run = old_run
            self.pi._REAL_AUTH = old_auth
            os.unlink(auth)
        self.assertTrue(res["completed"])
        cmd = calls[0][0]
        self.assertIn("--provider", cmd)
        self.assertEqual(cmd[cmd.index("--provider") + 1], "anthropic")
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-opus-4-8")
        self.assertEqual(cmd[cmd.index("--thinking") + 1], "medium")


class TestOpencodeOpus(unittest.TestCase):
    def setUp(self):
        self.openc = load("opencode")

    def test_missing_anthropic_auth_is_setup_needed(self):
        old = self.openc._ANTHROPIC_AUTH
        self.openc._ANTHROPIC_AUTH = "/definitely/missing/opencode/auth.json"
        try:
            res = self.openc.run("hi", "/tmp", "claude-opus-4-8", 5)
        finally:
            self.openc._ANTHROPIC_AUTH = old
        self.assertFalse(res["completed"])
        self.assertIn("SETUP-NEEDED", res["error"])
        self.assertIn("opencode auth login -p anthropic", res["error"])

    def test_constructs_gpt56_openai_medium_commands(self):
        variants = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
        old_run = self.openc.subprocess.run
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            tok = {"input": 3, "output": 2, "reasoning": 1, "cache": {"read": 0, "write": 0}, "total": 6}
            return FakeProc(stdout=json.dumps({"type": "step_finish", "part": {"tokens": tok}}) + "\n")

        self.openc.subprocess.run = fake_run
        with EnvPatch() as env:
            env["OPENAI_API_KEY"] = "must-be-stripped"
            try:
                for model in variants:
                    with self.subTest(model=model):
                        res = self.openc.run("hi", "/tmp", model, 5)
                        self.assertTrue(res["completed"])
                        cmd, kwargs = calls[-1]
                        self.assertEqual(cmd[cmd.index("-m") + 1], f"openai/{model}")
                        self.assertEqual(cmd[cmd.index("--variant") + 1], "medium")
                        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
                        self.assertEqual(res["token_basis"], "vendor_split")
            finally:
                self.openc.subprocess.run = old_run

    def test_constructs_anthropic_medium_command(self):
        old_run = self.openc.subprocess.run
        old_auth = self.openc._ANTHROPIC_AUTH
        fd, auth = tempfile.mkstemp()
        with os.fdopen(fd, "w") as fh:
            json.dump({"anthropic": {"type": "oauth", "access": "x"}}, fh)
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeProc(stdout='{"type":"step_finish","part":{"tokens":{"input":1,"output":2}}}\n')

        self.openc.subprocess.run = fake_run
        self.openc._ANTHROPIC_AUTH = auth
        try:
            res = self.openc.run("hi", "/tmp", "claude-opus-4-8", 5)
        finally:
            self.openc.subprocess.run = old_run
            self.openc._ANTHROPIC_AUTH = old_auth
            os.unlink(auth)
        self.assertTrue(res["completed"])
        cmd, kwargs = calls[0]
        self.assertEqual(cmd[cmd.index("-m") + 1], "anthropic/claude-opus-4-8")
        self.assertEqual(cmd[cmd.index("--variant") + 1], "medium")
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn("ANTHROPIC_API_KEY", kwargs["env"])


class TestClaudeOpus(unittest.TestCase):
    def setUp(self):
        self.claude = load("claude")

    def test_missing_anthropic_key_is_setup_needed(self):
        with EnvPatch() as env:
            env.pop("ANTHROPIC_API_KEY", None)
            res = self.claude.run("hi", "/tmp", "claude-opus-4-8", 5)
        self.assertFalse(res["completed"])
        self.assertIn("SETUP-NEEDED", res["error"])
        self.assertIn("ANTHROPIC_API_KEY", res["error"])

    def test_constructs_bare_first_party_medium_command(self):
        old_run = self.claude.subprocess.run
        old_resolve = self.claude._resolve_exe
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeProc(stdout='{"is_error":false,"num_turns":1,"result":"ok"}')

        self.claude.subprocess.run = fake_run
        self.claude._resolve_exe = lambda: "claude"
        with EnvPatch() as env:
            env["ANTHROPIC_API_KEY"] = "test-key"
            try:
                res = self.claude.run("hi", "/tmp", "claude-opus-4-8", 5)
            finally:
                self.claude.subprocess.run = old_run
                self.claude._resolve_exe = old_resolve
        self.assertTrue(res["completed"])
        cmd, kwargs = calls[0]
        self.assertIn("--bare", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-opus-4-8")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "medium")
        self.assertEqual(kwargs["env"]["ANTHROPIC_API_KEY"], "test-key")
        self.assertNotIn("ANTHROPIC_BASE_URL", kwargs["env"])


class TestCodexOpus(unittest.TestCase):
    def setUp(self):
        self.codex = load("codex")

    def test_missing_anthropic_key_is_setup_needed(self):
        with EnvPatch() as env:
            env.pop("ANTHROPIC_API_KEY", None)
            res = self.codex.run("hi", "/tmp", "claude-opus-4-8", 5)
        self.assertFalse(res["completed"])
        self.assertIn("SETUP-NEEDED", res["error"])
        self.assertIn("ANTHROPIC_API_KEY", res["error"])

    def test_constructs_gpt56_openai_codex_medium_commands(self):
        variants = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
        old_run = self.codex.subprocess.run
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            usage = {"input_tokens": 5, "cached_input_tokens": 1, "output_tokens": 3, "reasoning_output_tokens": 1}
            return FakeProc(stdout=json.dumps({"type": "turn.completed", "usage": usage}) + "\n")

        self.codex.subprocess.run = fake_run
        try:
            for model in variants:
                with self.subTest(model=model):
                    res = self.codex.run("hi", "/tmp", model, 5)
                    self.assertTrue(res["completed"])
                    cmd, kwargs = calls[-1]
                    self.assertIn('model_reasoning_effort="medium"', cmd)
                    self.assertIn('service_tier="default"', cmd)
                    self.assertEqual(cmd[cmd.index("-m") + 1], model)
                    self.assertIsNone(kwargs["env"])
                    self.assertEqual(res["token_basis"], "estimated")
                    self.assertIsNone(res["tokens_cache_write"])
        finally:
            self.codex.subprocess.run = old_run

    def test_constructs_bridge_medium_command(self):
        old_run = self.codex.subprocess.run
        old_reach = self.codex._bridge_reachable
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeProc(stdout='{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2}}\n')

        self.codex.subprocess.run = fake_run
        self.codex._bridge_reachable = lambda: True
        with EnvPatch() as env:
            env["ANTHROPIC_API_KEY"] = "test-key"
            env["DEEPSEEK_API_KEY"] = "other-secret"
            try:
                res = self.codex.run("hi", "/tmp", "claude-opus-4-8", 5)
            finally:
                self.codex.subprocess.run = old_run
                self.codex._bridge_reachable = old_reach
        self.assertTrue(res["completed"])
        cmd = calls[0][0]
        self.assertIn('model_providers.anthropic.base_url="http://localhost:4141/v1"', cmd)
        self.assertIn('model_providers.anthropic.env_key="ANTHROPIC_API_KEY"', cmd)
        self.assertIn('model_reasoning_effort="medium"', cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], "claude-opus-4-8")
        self.assertEqual(calls[0][1]["env"]["ANTHROPIC_API_KEY"], "openbench-bridge-placeholder")
        self.assertNotIn("DEEPSEEK_API_KEY", calls[0][1]["env"])


class TestCursorOpus(unittest.TestCase):
    def setUp(self):
        self.cursor = load("cursor")

    def test_missing_cursor_auth_is_setup_needed_for_opus(self):
        old_auth = self.cursor._CURSOR_AUTH
        self.cursor._CURSOR_AUTH = "/definitely/missing/cursor/auth.json"
        with EnvPatch() as env:
            env.pop("CURSOR_API_KEY", None)
            env["BENCH_IN_CONTAINER"] = "1"
            try:
                res = self.cursor.run("hi", "/tmp", "claude-opus-4-8", 5)
            finally:
                self.cursor._CURSOR_AUTH = old_auth
        self.assertFalse(res["completed"])
        self.assertIn("SETUP-NEEDED", res["error"])
        self.assertIn("CURSOR_API_KEY", res["error"])

    def test_constructs_gpt56_medium_models(self):
        variants = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
        old_run = self.cursor.subprocess.run
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeProc(stdout='{"result":"ok","usage":{"inputTokens":1,"outputTokens":2}}')

        self.cursor.subprocess.run = fake_run
        try:
            for model in variants:
                with self.subTest(model=model):
                    res = self.cursor.run("hi", "/tmp", model, 5)
                    self.assertTrue(res["completed"])
                    cmd = calls[-1][0]
                    self.assertEqual(cmd[cmd.index("--model") + 1], f"{model}-medium")
                    self.assertEqual(res["token_basis"], "harness_reported")
        finally:
            self.cursor.subprocess.run = old_run

    def test_constructs_medium_thinking_model_with_api_key_fallback(self):
        old_run = self.cursor.subprocess.run
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeProc(stdout='{"result":"ok","usage":{"inputTokens":1,"outputTokens":2}}')

        self.cursor.subprocess.run = fake_run
        with EnvPatch() as env:
            env["CURSOR_API_KEY"] = "test-key"
            try:
                res = self.cursor.run("hi", "/tmp", "claude-opus-4-8", 5)
            finally:
                self.cursor.subprocess.run = old_run
        self.assertTrue(res["completed"])
        cmd = calls[0][0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-opus-4-8-thinking-medium")
        self.assertIn("--trust", cmd)
        self.assertIn("--force", cmd)


if __name__ == "__main__":
    unittest.main()
