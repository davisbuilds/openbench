#!/usr/bin/env python3
"""Devin adapter model mapping and command construction tests.

Locks in the Jul 2026 verified behavior (devin v3000.2.17, Max plan):
`--model` accepts dashed menu UIDs and is passed EXPLICITLY for the wired
canonicals; the legacy gpt-5.5 canonical still omits --model (account
default). usage_raw carries the devin-cloud serving-path marker.
"""

import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER_PATH = os.path.join(BENCH_DIR, "adapters", "devin.py")


def load_devin():
    spec = importlib.util.spec_from_file_location("devin_adapter_test", ADAPTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeProc:
    returncode = 0
    stdout = "done"
    stderr = ""


class TestModels(unittest.TestCase):
    def setUp(self):
        self.devin = load_devin()

    def test_canonical_to_uid_mapping(self):
        self.assertEqual(self.devin.MODELS["gpt-5.6-sol"], "gpt-5-6-sol-medium")
        self.assertEqual(self.devin.MODELS["grok-4.5"], "grok-4-5-medium")
        self.assertEqual(self.devin.MODELS["glm-5.2"], "glm-5-2")
        # Legacy lane: account default, no --model.
        self.assertIsNone(self.devin.MODELS["gpt-5.5-medium"])

    def test_excluded_models_rejected(self):
        # deepseek is absent from devin's menu; kimi is k2.7 not k3 — both are
        # excluded, so their canonicals must fail closed as unsupported.
        for model in ("deepseek-v4-flash", "kimi-k3"):
            res = self.devin.run("x", "/tmp", model, 5)
            self.assertFalse(res["completed"])
            self.assertIn("unsupported-model", res["error"])

    def _run_and_capture_cmd(self, model):
        with mock.patch.object(self.devin.subprocess, "run",
                               return_value=FakeProc()) as m:
            res = self.devin.run("do it", "/tmp", model, 5)
        (cmd,), _kwargs = m.call_args
        return cmd, res

    def test_wired_canonicals_pass_model_flag(self):
        for model, uid in (("gpt-5.6-sol", "gpt-5-6-sol-medium"),
                           ("grok-4.5", "grok-4-5-medium"),
                           ("glm-5.2", "glm-5-2")):
            cmd, res = self._run_and_capture_cmd(model)
            self.assertTrue(res["completed"])
            self.assertIn("--model", cmd)
            self.assertEqual(cmd[cmd.index("--model") + 1], uid)
            # Instruction stays behind the -- sentinel.
            self.assertEqual(cmd[-2:], ["--", "do it"])

    def test_legacy_gpt55_omits_model_flag(self):
        cmd, res = self._run_and_capture_cmd("gpt-5.5-medium")
        self.assertTrue(res["completed"])
        self.assertNotIn("--model", cmd)


class TestServingPathMarker(unittest.TestCase):
    def test_usage_raw_carries_devin_cloud_marker(self):
        devin = load_devin()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({
                "final_metrics": {
                    "total_prompt_tokens": 100,
                    "total_completion_tokens": 10,
                    "total_cached_tokens": 60,
                },
                "steps": [{"metrics": {"prompt_tokens": 90}}],
            }, fh)
            path = fh.name
        try:
            tokens, turns, usage = devin._parse_export_with_usage(path)
        finally:
            os.unlink(path)
        self.assertEqual(tokens, 50)  # 100 - 60 + 10 (fresh, cache excluded)
        self.assertEqual(turns, 1)
        self.assertEqual(usage["usage_raw"]["serving_path"], "devin-cloud")
        self.assertEqual(usage["token_basis"], "estimated")


if __name__ == "__main__":
    unittest.main()
