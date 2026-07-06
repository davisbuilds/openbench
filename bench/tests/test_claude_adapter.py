#!/usr/bin/env python3
"""Unit tests for the `claude` open-model adapter and its docker plumbing.

No live CLI / network / Docker calls: parsing is exercised on canned
`--output-format json` payloads, and model-gating is exercised by manipulating
the process environment.
"""

import importlib.util
import os
import sys
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER_PATH = os.path.join(BENCH_DIR, "adapters", "claude.py")

sys.path.insert(0, BENCH_DIR)

import docker_exec  # noqa: E402


def _load_claude():
    spec = importlib.util.spec_from_file_location("bench_adapter_claude", ADAPTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


claude = _load_claude()


class TestParseJson(unittest.TestCase):
    def test_model_usage_disjoint_fresh_basis(self):
        # Anthropic-style fields are DISJOINT: fresh = input + cacheCreation +
        # output; cacheRead excluded. modelUsage is preferred over usage.
        payload = (
            '{"type":"result","is_error":false,"num_turns":3,'
            '"result":"all done",'
            '"usage":{"input_tokens":1,"output_tokens":1},'
            '"modelUsage":{"deepseek-v4-flash":{"inputTokens":100,'
            '"outputTokens":40,"cacheReadInputTokens":999,'
            '"cacheCreationInputTokens":10}}}'
        )
        tokens, turns, tail, ok = claude._parse_json(payload)
        self.assertEqual(tokens, 150)  # 100 + 10 + 40 (cacheRead 999 excluded)
        self.assertEqual(turns, 3)
        self.assertEqual(tail, "all done")
        self.assertTrue(ok)

    def test_sums_multiple_models(self):
        payload = (
            '{"num_turns":2,"is_error":false,"result":"x",'
            '"modelUsage":{"a":{"inputTokens":10,"outputTokens":5},'
            '"b":{"inputTokens":20,"outputTokens":1,'
            '"cacheCreationInputTokens":4}}}'
        )
        tokens, _, _, _ = claude._parse_json(payload)
        self.assertEqual(tokens, 10 + 5 + 20 + 1 + 4)

    def test_falls_back_to_top_level_usage(self):
        payload = (
            '{"num_turns":1,"is_error":false,"result":"ok",'
            '"usage":{"input_tokens":50,"output_tokens":8,'
            '"cache_creation_input_tokens":2,"cache_read_input_tokens":99}}'
        )
        tokens, turns, tail, ok = claude._parse_json(payload)
        self.assertEqual(tokens, 60)  # 50 + 2 + 8 (cache_read 99 excluded)
        self.assertEqual(turns, 1)

    def test_is_error_true_reported(self):
        payload = '{"num_turns":1,"is_error":true,"result":"boom"}'
        tokens, turns, tail, ok = claude._parse_json(payload)
        self.assertIsNone(tokens)
        self.assertEqual(turns, 1)
        self.assertFalse(ok)

    def test_garbage_is_defensive(self):
        tokens, turns, tail, ok = claude._parse_json("not json at all")
        self.assertIsNone(tokens)
        self.assertIsNone(turns)
        self.assertEqual(tail, "")
        self.assertIsNone(ok)

    def test_result_line_amid_noise(self):
        stdout = "startup log line\n" + '{"num_turns":1,"is_error":false,"result":"hi"}'
        tokens, turns, tail, ok = claude._parse_json(stdout)
        self.assertEqual(tail, "hi")
        self.assertTrue(ok)


class TestModelGating(unittest.TestCase):
    def test_frontier_model_unsupported(self):
        # This adapter must NEVER accept a subscription/frontier model.
        res = claude.run("do it", "/tmp", "gpt-5.5-medium", 5)
        self.assertFalse(res["completed"])
        self.assertIn("unsupported-model", res["error"])

    def test_unknown_model_unsupported(self):
        res = claude.run("do it", "/tmp", "not-a-model", 5)
        self.assertFalse(res["completed"])
        self.assertIn("unsupported-model", res["error"])

    def test_missing_key_is_setup_needed(self):
        orig = dict(os.environ)
        os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            res = claude.run("do it", "/tmp", "deepseek-v4-flash", 5)
        finally:
            os.environ.clear()
            os.environ.update(orig)
        self.assertFalse(res["completed"])
        self.assertIn("SETUP-NEEDED", res["error"])
        self.assertIn("DEEPSEEK_API_KEY", res["error"])


class TestDockerAuthMounts(unittest.TestCase):
    def test_claude_mounts_nothing(self):
        # claude runs open models only; ~/.claude must never be mounted so a
        # container run can't touch the user's Anthropic subscription.
        self.assertIn("claude", docker_exec.AUTH_MOUNTS)
        self.assertEqual(docker_exec.AUTH_MOUNTS["claude"], [])
        self.assertEqual(docker_exec._auth_mount_args("claude"), [])


if __name__ == "__main__":
    unittest.main()
