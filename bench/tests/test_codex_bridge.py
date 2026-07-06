#!/usr/bin/env python3
"""Tests for the codex open-model bridge URL selection.

Open models (DeepSeek/Z.ai/Moonshot) run through a host-side Responses<->Chat
bridge because codex 0.142 requires the Responses API but those vendors are
chat-only. The adapter must point codex at the bridge, choosing localhost on the
host lane and host.docker.internal inside the bench container (BENCH_IN_CONTAINER
set by entry.py), honour a BENCH_BRIDGE_PORT override, and fail fast with a
SETUP-NEEDED dict when the bridge port is unreachable.
"""

import importlib.util
import os
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTERS_DIR = os.path.join(BENCH_DIR, "adapters")


def load_codex():
    spec = importlib.util.spec_from_file_location(
        "to_codex", os.path.join(ADAPTERS_DIR, "codex.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBridgeUrl(unittest.TestCase):
    def setUp(self):
        self.codex = load_codex()
        # Snapshot and clear the env vars we mutate so tests are independent.
        self._saved = {k: os.environ.get(k)
                       for k in ("BENCH_IN_CONTAINER", "BENCH_BRIDGE_PORT")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_host_lane_uses_localhost(self):
        os.environ.pop("BENCH_IN_CONTAINER", None)
        self.assertEqual(self.codex._bridge_host(), "localhost")
        self.assertEqual(self.codex._bridge_base_url(),
                         "http://localhost:4141/v1")

    def test_container_lane_uses_host_docker_internal(self):
        os.environ["BENCH_IN_CONTAINER"] = "1"
        self.assertEqual(self.codex._bridge_host(), "host.docker.internal")
        self.assertEqual(self.codex._bridge_base_url(),
                         "http://host.docker.internal:4141/v1")

    def test_port_override(self):
        os.environ["BENCH_BRIDGE_PORT"] = "9000"
        self.assertEqual(self.codex._bridge_port(), 9000)
        self.assertEqual(self.codex._bridge_base_url(),
                         "http://localhost:9000/v1")

    def test_base_url_ends_in_v1_for_responses_suffix(self):
        # codex appends /responses to base_url; /v1 tail yields /v1/responses.
        self.assertTrue(self.codex._bridge_base_url().endswith("/v1"))


class TestBridgeGating(unittest.TestCase):
    """run() must gate open models on the bridge being reachable."""

    def setUp(self):
        self.codex = load_codex()

    def test_missing_env_key_is_setup_needed(self):
        saved = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            res = self.codex.run("hi", "/tmp", "deepseek-v4-flash", 5)
            self.assertFalse(res["completed"])
            self.assertIn("SETUP-NEEDED", res["error"])
            self.assertIn("DEEPSEEK_API_KEY", res["error"])
        finally:
            if saved is not None:
                os.environ["DEEPSEEK_API_KEY"] = saved

    def test_bridge_down_is_setup_needed(self):
        # Force key present + an unused port so the reachability probe fails.
        os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
        saved_key = os.environ.get("DEEPSEEK_API_KEY")
        saved_port = os.environ.get("BENCH_BRIDGE_PORT")
        os.environ["DEEPSEEK_API_KEY"] = saved_key or "test-key"
        os.environ["BENCH_BRIDGE_PORT"] = "1"  # nothing listens on TCP/1
        try:
            res = self.codex.run("hi", "/tmp", "deepseek-v4-flash", 5)
            self.assertFalse(res["completed"])
            self.assertIn("SETUP-NEEDED", res["error"])
            self.assertIn("bridge unreachable", res["error"])
        finally:
            if saved_port is None:
                os.environ.pop("BENCH_BRIDGE_PORT", None)
            else:
                os.environ["BENCH_BRIDGE_PORT"] = saved_port


if __name__ == "__main__":
    unittest.main()
