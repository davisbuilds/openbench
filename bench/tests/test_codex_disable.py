#!/usr/bin/env python3
"""Tests that codex disables server-driven feature tools on every command path."""

import importlib.util
import json
import os
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTERS_DIR = os.path.join(BENCH_DIR, "adapters")


def load_codex():
    spec = importlib.util.spec_from_file_location(
        "test_codex_disable", os.path.join(ADAPTERS_DIR, "codex.py"))
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


def assert_feature_tools_disabled(testcase, cmd):
    disabled = [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == "--disable"]
    testcase.assertIn("apps", disabled)
    testcase.assertIn("plugins", disabled)
    testcase.assertIn("multi_agent", disabled)


class TestCodexFeatureToolDisable(unittest.TestCase):
    def setUp(self):
        self.codex = load_codex()

    def _capture_run(self):
        old_run = self.codex.subprocess.run
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            usage = {
                "input_tokens": 5,
                "cached_input_tokens": 0,
                "output_tokens": 2,
                "reasoning_output_tokens": 0,
            }
            return FakeProc(stdout=json.dumps({"type": "turn.completed", "usage": usage}) + "\n")

        self.codex.subprocess.run = fake_run
        return old_run, calls

    def test_first_party_command_disables_feature_tools(self):
        old_run, calls = self._capture_run()
        try:
            res = self.codex.run("hi", "/tmp", "gpt-5.5-medium", 5)
        finally:
            self.codex.subprocess.run = old_run

        self.assertTrue(res["completed"])
        assert_feature_tools_disabled(self, calls[0][0])

    def test_open_model_command_disables_feature_tools(self):
        old_run, calls = self._capture_run()
        old_reach = self.codex._bridge_reachable
        self.codex._bridge_reachable = lambda: True
        with EnvPatch() as env:
            env["DEEPSEEK_API_KEY"] = "test-key"
            try:
                res = self.codex.run("hi", "/tmp", "deepseek-v4-flash", 5)
            finally:
                self.codex.subprocess.run = old_run
                self.codex._bridge_reachable = old_reach

        self.assertTrue(res["completed"])
        assert_feature_tools_disabled(self, calls[0][0])


if __name__ == "__main__":
    unittest.main()
