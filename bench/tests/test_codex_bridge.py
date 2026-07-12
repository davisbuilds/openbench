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
import sys
import types
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTERS_DIR = os.path.join(BENCH_DIR, "adapters")
BRIDGE_DIR = os.path.join(BENCH_DIR, "bridge")


def load_codex():
    spec = importlib.util.spec_from_file_location(
        "to_codex", os.path.join(ADAPTERS_DIR, "codex.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_bridge_hooks():
    """Load bench/bridge/hooks.py with litellm stubbed (it lives in a separate
    venv, not the test interpreter). Only the pure sanitizers are exercised."""
    for name in ("litellm", "litellm.integrations",
                 "litellm.integrations.custom_logger", "litellm._logging"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["litellm.integrations.custom_logger"].CustomLogger = object
    sys.modules["litellm._logging"].verbose_proxy_logger = types.SimpleNamespace(
        info=lambda *a, **k: None)
    spec = importlib.util.spec_from_file_location(
        "bridge_hooks", os.path.join(BRIDGE_DIR, "hooks.py"))
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
        old_keys = self.codex._KEYS_ENV
        self.codex._KEYS_ENV = "/definitely/missing/openbench-keys.env"
        try:
            res = self.codex.run("hi", "/tmp", "deepseek-v4-flash", 5)
            self.assertFalse(res["completed"])
            self.assertIn("SETUP-NEEDED", res["error"])
            self.assertIn("DEEPSEEK_API_KEY", res["error"])
        finally:
            self.codex._KEYS_ENV = old_keys
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


class TestThinkingNormalize(unittest.TestCase):
    """Bridge thinking parity: normalize codex requests before LiteLLM routing."""

    def setUp(self):
        self.h = load_bridge_hooks()

    def test_glm52_enables_thinking_and_high_effort(self):
        data = {"model": "glm-5.2", "reasoning": {"effort": "medium"}}
        self.h.normalize_thinking(data)
        self.assertEqual(data["thinking"], {"type": "enabled", "clear_thinking": False})
        self.assertEqual(data["reasoning"], {"effort": "high"})
        self.assertEqual(data["reasoning_effort"], "high")
        self.assertEqual(data["extra_body"]["reasoning_effort"], "high")

    def test_default_thinking_models_enable_without_effort(self):
        for model in ("glm-4.7-flash", "deepseek-v4-flash", "kimi-k2.7-code"):
            data = {"model": model, "reasoning": {"effort": "medium"}, "reasoning_effort": "medium"}
            self.h.normalize_thinking(data)
            self.assertEqual(data["thinking"]["type"], "enabled")
            if model != "deepseek-v4-flash":
                self.assertEqual(data["extra_body"]["thinking"]["type"], "enabled")
            self.assertNotIn("reasoning", data)
            self.assertNotIn("reasoning_effort", data)

    def test_chat_thinking_reapplies_after_responses_conversion(self):
        cases = {
            "zai/glm-5.2": "high",
            "zai/glm-4.7-flash": None,
            "deepseek/deepseek-v4-flash": None,
            "moonshot/kimi-k2.7-code": None,
        }
        for model, effort in cases.items():
            kwargs = {"model": model, "reasoning_effort": "medium"}
            self.h.normalize_chat_thinking(model, kwargs)
            self.assertEqual(kwargs["thinking"]["type"], "enabled")
            if "deepseek" not in model:
                self.assertEqual(kwargs["extra_body"]["thinking"]["type"], "enabled")
            if effort:
                self.assertEqual(kwargs["reasoning_effort"], effort)
                self.assertEqual(kwargs["extra_body"]["reasoning_effort"], effort)
            else:
                self.assertNotIn("reasoning_effort", kwargs)


class TestToolSanitize(unittest.TestCase):
    """Z.ai-route tool sanitizing: keep/coerce function tools, drop the rest."""

    def setUp(self):
        self.h = load_bridge_hooks()
        self.tools = [
            {"type": "function", "name": "exec_command", "description": "run",
             "strict": True, "parameters": {"type": "object"}},
            {"type": "namespace", "name": "multi_agent_v1",
             "description": "bundle", "tools": []},
            {"type": "web_search", "external_web_access": True},
            {"type": "image_generation", "output_format": "png"},
            {"type": "custom", "name": "freeform", "description": "d",
             "parameters": {"type": "object"}},
        ]

    def test_route_detection(self):
        self.assertTrue(self.h._is_zai_route("glm-5.2"))
        self.assertTrue(self.h._is_zai_route("glm-4.7-flash"))
        self.assertFalse(self.h._is_zai_route("deepseek-v4-flash"))
        self.assertFalse(self.h._is_zai_route(None))
        for model in ("claude-opus-4-8", "glm-5.2", "glm-4.7-flash", "deepseek-v4-flash", "kimi-k2.7-code"):
            self.assertTrue(self.h._is_chat_vendor_route(model))
        self.assertFalse(self.h._is_chat_vendor_route("gpt-5.4"))

    def test_keeps_function_coerces_and_drops(self):
        data = {"model": "glm-5.2", "tools": list(self.tools)}
        self.h.sanitize_tools(data)
        names = [t["name"] for t in data["tools"]]
        # exec_command kept; freeform (custom w/ params) coerced; namespace,
        # web_search, image_generation dropped.
        self.assertEqual(names, ["exec_command", "freeform"])
        self.assertTrue(all(t["type"] == "function" for t in data["tools"]))

    def test_all_function_tools_untouched(self):
        only_fns = [t for t in self.tools if t["type"] == "function"]
        data = {"model": "glm-5.2", "tools": list(only_fns)}
        self.h.sanitize_tools(data)
        self.assertEqual(data["tools"], only_fns)

    def test_pure_coercions_are_written_back(self):
        data = {"model": "kimi-k2.7-code", "tools": [
            {"type": "custom", "name": "freeform", "description": "d",
             "parameters": {"type": "object"}},
        ]}
        self.h.sanitize_tools(data)
        self.assertEqual(data["tools"], [
            {"type": "function", "name": "freeform", "description": "d",
             "parameters": {"type": "object"}},
        ])

    def test_no_tools_key_is_safe(self):
        data = {"model": "glm-5.2"}
        self.h.sanitize_tools(data)  # must not raise
        self.assertNotIn("tools", data)


class TestDeepSeekReasoningPreserve(unittest.TestCase):
    """DeepSeek thinking mode requires reasoning_content on tool-call turns."""

    def setUp(self):
        self.h = load_bridge_hooks()

    def test_moves_merged_reasoning_content_to_reasoning_content_field(self):
        messages = [
            {"role": "assistant", "content": "hidden chain", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
        self.h.preserve_deepseek_reasoning_content("deepseek-v4-flash", messages)
        self.assertEqual(messages[0]["reasoning_content"], "hidden chain")
        self.assertIsNone(messages[0]["content"])

    def test_merges_previous_reasoning_assistant_into_tool_call_message(self):
        messages = [
            {"role": "assistant", "content": "hidden chain"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "exec", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
        self.h.preserve_deepseek_reasoning_content("deepseek-v4-flash", messages)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["reasoning_content"], "hidden chain")
        self.assertIsNone(messages[0]["content"])

    def test_custom_deepseek_model_group_is_thinking_active(self):
        self.assertTrue(self.h._deepseek_thinking_mode_active_for_bridge(
            "deepseek-v4-flash", {"thinking": {"type": "enabled"}}))
        self.assertTrue(self.h._deepseek_thinking_mode_active_for_bridge(
            "deepseek/deepseek-v4-flash", {"thinking": {"type": "enabled"}}))
        self.assertFalse(self.h._deepseek_thinking_mode_active_for_bridge(
            "deepseek-v4-flash", {"thinking": {"type": "disabled"}}))


if __name__ == "__main__":
    unittest.main()
