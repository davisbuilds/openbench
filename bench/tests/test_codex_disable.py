#!/usr/bin/env python3
"""Tests that stock Codex keeps factory features and isolates personal config."""

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


def assert_factory_features(testcase, cmd, kwargs):
    testcase.assertNotIn("--disable", cmd)
    testcase.assertNotEqual(kwargs["env"]["CODEX_HOME"],
                            os.path.expanduser("~/.codex"))


class TestCodexConfigIsolation(unittest.TestCase):
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

    def test_first_party_command_uses_isolated_factory_config(self):
        old_run, calls = self._capture_run()
        try:
            res = self.codex.run("hi", "/tmp", "gpt-5.5-medium", 5)
        finally:
            self.codex.subprocess.run = old_run
        self.assertTrue(res["completed"])
        assert_factory_features(self, calls[0][0], calls[0][1])

    def test_open_model_command_uses_isolated_factory_config(self):
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
        assert_factory_features(self, calls[0][0], calls[0][1])


if __name__ == "__main__":
    unittest.main()
