#!/usr/bin/env python3
"""Unit tests for the Grok Build BYOK open-model adapter.

No live Grok/network calls: subprocess.run and CLI discovery are stubbed.  The
stream parser is exercised against the captured DeepSeek probe fixture.
"""

import importlib.util
import os
import tempfile
import tomllib
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER_PATH = os.path.join(BENCH_DIR, "adapters", "grokbuild.py")
FIXTURE_PATH = os.path.join(BENCH_DIR, "tests", "fixtures", "grokbuild_stream_deepseek.jsonl")
LOG_FIXTURE_PATH = os.path.join(BENCH_DIR, "tests", "fixtures", "grokbuild_unified_multiturn.jsonl")


def _load_grokbuild():
    spec = importlib.util.spec_from_file_location("bench_adapter_grokbuild", ADAPTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


grokbuild = _load_grokbuild()


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


class TestStreamParsing(unittest.TestCase):
    def test_captured_stream_fixture(self):
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            stdout = fh.read()
        tokens, turns, tail = grokbuild._parse_stream(stdout)
        self.assertIsNone(tokens)  # usage is not present in streaming-json events
        self.assertEqual(turns, 1)
        self.assertEqual(tail, "OK")

    def test_future_usage_shape_is_counted(self):
        stdout = (
            '{"type":"text","data":"hi"}\n'
            '{"type":"end","usage":{"input_tokens":10,"output_tokens":3}}\n'
        )
        tokens, turns, tail = grokbuild._parse_stream(stdout)
        self.assertEqual(tokens, 13)
        self.assertEqual(turns, 1)
        self.assertEqual(tail, "hi")

    def test_log_usage_excludes_cached_prompt_tokens(self):
        home = tempfile.mkdtemp(prefix="grokbuild_log_test_")
        try:
            log_dir = os.path.join(home, "logs")
            os.makedirs(log_dir)
            with open(os.path.join(log_dir, "unified.jsonl"), "w", encoding="utf-8") as fh:
                fh.write('{"msg":"other"}\n')
                fh.write(
                    '{"msg":"shell.turn.inference_done",'
                    '"ctx":{"prompt_tokens":100,"cached_prompt_tokens":40,'
                    '"completion_tokens":7,"reasoning_tokens":2}}\n'
                )
            usage = grokbuild._parse_log_usage(home)
            self.assertEqual(usage["tokens"], 67)
            self.assertEqual(usage["tokens_input_uncached"], 60)
            self.assertEqual(usage["tokens_cache_read"], 40)
            self.assertEqual(usage["tokens_output"], 7)
            self.assertEqual(usage["tokens_reasoning"], 2)
        finally:
            import shutil
            shutil.rmtree(home, ignore_errors=True)

    def test_multi_turn_log_usage_sums_per_call_counters(self):
        home = tempfile.mkdtemp(prefix="grokbuild_log_test_")
        try:
            log_dir = os.path.join(home, "logs")
            os.makedirs(log_dir)
            with open(LOG_FIXTURE_PATH, encoding="utf-8") as src:
                data = src.read()
            with open(os.path.join(log_dir, "unified.jsonl"), "w", encoding="utf-8") as dst:
                dst.write(data)
            usage = grokbuild._parse_log_usage(home)
            self.assertEqual(usage["tokens"], 1898)
            self.assertEqual(usage["tokens_input_uncached"], 1522)
            self.assertEqual(usage["tokens_cache_read"], 46592)
            self.assertEqual(usage["tokens_output"], 376)
            self.assertEqual(usage["tokens_reasoning"], 80)
            self.assertEqual(usage["turns"], 5)
        finally:
            import shutil
            shutil.rmtree(home, ignore_errors=True)


class TestConfigAndGating(unittest.TestCase):
    def test_config_toml_for_open_model(self):
        cfg = grokbuild._config_toml(
            "deepseek-v4-flash", grokbuild.OPEN_MODELS["deepseek-v4-flash"])
        self.assertIn('[model."deepseek-v4-flash"]', cfg)
        parsed = tomllib.loads(cfg)
        self.assertIn("deepseek-v4-flash", parsed["model"])
        self.assertEqual(parsed["models"]["default"], "deepseek-v4-flash")
        self.assertIn('model = "deepseek-v4-flash"', cfg)
        self.assertIn('base_url = "https://api.deepseek.com"', cfg)
        self.assertIn('env_key = "DEEPSEEK_API_KEY"', cfg)
        self.assertIn('[models]\ndefault = "deepseek-v4-flash"', cfg)
        self.assertIn('api_backend = "chat_completions"', cfg)
        self.assertIn('auth_scheme = "bearer"', cfg)
        self.assertIn('stream_tool_calls = false', cfg)
        self.assertIn('max_completion_tokens = 65536', cfg)
        self.assertNotIn('temperature =', cfg)
        self.assertNotIn('top_p =', cfg)
        self.assertEqual(parsed["models"]["default_reasoning_effort"], "medium")
        self.assertIn('[model."grok-build"]', cfg)
        self.assertEqual(parsed["models"]["web_search"], "deepseek-v4-flash")
        self.assertEqual(parsed["models"]["session_summary"], "deepseek-v4-flash")
        self.assertEqual(parsed["models"]["image_description"], "deepseek-v4-flash")
        self.assertEqual(parsed["ui"]["fork_secondary_model"], "deepseek-v4-flash")
        self.assertEqual(parsed["compaction"]["memory_flush"]["flush_model"], "deepseek-v4-flash")
        self.assertEqual(parsed["goal"]["planner_model"], "deepseek-v4-flash")
        self.assertEqual(parsed["goal"]["strategist_model"], "deepseek-v4-flash")
        self.assertEqual(parsed["goal"]["skeptic_models"], ["deepseek-v4-flash"])
        self.assertNotIn('[compat.claude]', cfg)
        self.assertNotIn('[compat.cursor]', cfg)
        self.assertFalse(parsed["subagents"]["enabled"])

    def test_proxy_rewrites_every_route_to_per_cell_proxy_url(self):
        expected = {
            "glm-5.2": "chat/zai/api/paas/v4",
            "deepseek-v4-flash": "chat/deepseek",
            "kimi-k2.7-code": "chat/moonshot/v1",
            "gpt-5.6": "openai/v1",
        }
        with EnvPatch() as env:
            env.update({
                "OPENBENCH_PROXY": "1",
                "OPENBENCH_PROXY_BASE_URL": "http://proxy.test:1234",
                "OPENBENCH_PROXY_CELL_TOKEN": "cell-token",
            })
            for model, suffix in expected.items():
                proxied = grokbuild._proxied_spec(grokbuild.OPEN_MODELS[model])
                self.assertEqual(proxied["base_url"], f"http://proxy.test:1234/cell/cell-token/{suffix}")

    def test_subbridge_route_rejects_url_embedded_credentials(self):
        with EnvPatch() as env:
            env["CLIPROXYAPI_BASE_URL"] = "http://user:secret" + "@127.0.0.1:8317/v1"
            with self.assertRaisesRegex(ValueError, "must not contain"):
                grokbuild._resolved_spec(grokbuild.OPEN_MODELS["gpt-5.6"])

    def test_invalid_subbridge_url_returns_structured_setup_error(self):
        with EnvPatch() as env:
            env["CLIPROXYAPI_BASE_URL"] = "http://127.0.0.1:8317/v1?secret=value"
            result = grokbuild.run("hi", "/tmp", "gpt-5.6", 5)
        self.assertFalse(result["completed"])
        self.assertIn("SETUP-NEEDED", result["error"])
        self.assertIsNone(result["cmd"])

    def test_subbridge_route_rejects_query_bearing_base_url(self):
        with EnvPatch() as env:
            env["CLIPROXYAPI_BASE_URL"] = "http://127.0.0.1:8317/v1?api-version=test"
            with self.assertRaisesRegex(ValueError, "query or fragment"):
                grokbuild._resolved_spec(grokbuild.OPEN_MODELS["gpt-5.6"])

    def test_subbridge_route_defaults_to_local_cliproxyapi(self):
        with EnvPatch() as env:
            env.pop("CLIPROXYAPI_BASE_URL", None)
            cfg = grokbuild._config_toml("gpt-5.6", grokbuild._resolved_spec(grokbuild.OPEN_MODELS["gpt-5.6"]))
        entry = tomllib.loads(cfg)["model"]["gpt-5.6"]
        self.assertEqual(entry["base_url"], "http://127.0.0.1:8317/v1")
        self.assertEqual(entry["env_key"], "CLIPROXYAPI_API_KEY")

    def test_subbridge_uses_docker_host_address_in_container(self):
        for configured in (None, "http://127.0.0.1:8317/v1", "http://localhost:8317/v1"):
            with self.subTest(configured=configured), EnvPatch() as env:
                env["BENCH_IN_CONTAINER"] = "1"
                if configured is None:
                    env.pop("CLIPROXYAPI_BASE_URL", None)
                else:
                    env["CLIPROXYAPI_BASE_URL"] = configured
                resolved = grokbuild._resolved_spec(grokbuild.OPEN_MODELS["gpt-5.6"])
            self.assertEqual(resolved["base_url"], "http://host.docker.internal:8317/v1")

    def test_dotted_model_aliases_are_quoted_toml_keys(self):
        for model, spec in grokbuild.OPEN_MODELS.items():
            parsed = tomllib.loads(grokbuild._config_toml(model, spec))
            self.assertIn(model, parsed["model"])
            self.assertEqual(parsed["models"]["default"], model)

    def test_missing_key_is_setup_needed_before_cli_check(self):
        old_resolve = grokbuild._resolve_exe
        try:
            grokbuild._resolve_exe = lambda: None
            with EnvPatch() as env:
                env.pop("DEEPSEEK_API_KEY", None)
                res = grokbuild.run("hi", "/tmp", "deepseek-v4-flash", 5)
        finally:
            grokbuild._resolve_exe = old_resolve
        self.assertFalse(res["completed"])
        self.assertIn("SETUP-NEEDED", res["error"])
        self.assertIn("DEEPSEEK_API_KEY", res["error"])
        self.assertIsNone(res["cmd"])

    def test_missing_cli_is_setup_needed(self):
        old_resolve = grokbuild._resolve_exe
        try:
            grokbuild._resolve_exe = lambda: None
            with EnvPatch() as env:
                env["DEEPSEEK_API_KEY"] = "test-key"
                res = grokbuild.run("hi", "/tmp", "deepseek-v4-flash", 5)
        finally:
            grokbuild._resolve_exe = old_resolve
        self.assertFalse(res["completed"])
        self.assertIn("SETUP-NEEDED", res["error"])
        self.assertIn("install Grok Build CLI", res["error"])

    def test_unknown_model_unsupported(self):
        res = grokbuild.run("hi", "/tmp", "glm-4.7-flash", 5)
        self.assertFalse(res["completed"])
        self.assertIn("unsupported-model", res["error"])


class TestRunConstruction(unittest.TestCase):
    def test_constructs_cmd_and_config_for_each_open_model(self):
        old_run = grokbuild.subprocess.run
        old_resolve = grokbuild._resolve_exe
        calls = []
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            fixture = fh.read()

        def fake_run(cmd, **kwargs):
            home = kwargs["env"]["HOME"]
            with open(os.path.join(home, ".grok", "config.toml"), encoding="utf-8") as cfg_fh:
                cfg = cfg_fh.read()
            log_dir = os.path.join(home, ".grok", "logs")
            os.makedirs(log_dir)
            with open(os.path.join(log_dir, "unified.jsonl"), "w", encoding="utf-8") as fh:
                fh.write(
                    '{"msg":"shell.turn.inference_done",'
                    '"ctx":{"prompt_tokens":100,"cached_prompt_tokens":40,'
                    '"completion_tokens":7,"reasoning_tokens":2}}\n'
                )
            calls.append((cmd, kwargs, cfg))
            return FakeProc(stdout=fixture)

        grokbuild.subprocess.run = fake_run
        grokbuild._resolve_exe = lambda: "/usr/local/bin/grok"
        try:
            with EnvPatch() as env:
                env.pop("CLIPROXYAPI_BASE_URL", None)
                env.update({
                    "DEEPSEEK_API_KEY": "deepseek-test",
                    "ZAI_API_KEY": "zai-test",
                    "MOONSHOT_API_KEY": "moonshot-test",
                    # This sentinel must be stripped from the gpt-5.6 child.
                    "OPENAI_API_KEY": "must-not-reach-subbridge",
                })
                for model in ("deepseek-v4-flash", "glm-5.2", "kimi-k2.7-code", "gpt-5.6"):
                    res = grokbuild.run("do it", "/tmp", model, 10)
                    self.assertTrue(res["completed"])
                    self.assertEqual(res["output_tail"], "OK")
                    self.assertEqual(res["turns"], 1)
                    self.assertEqual(res["tokens"], 67)
                    self.assertEqual(res["tokens_input_uncached"], 60)
                    self.assertEqual(res["tokens_cache_read"], 40)
                    self.assertEqual(res["tokens_output"], 7)
                    self.assertEqual(res["tokens_reasoning"], 2)
        finally:
            grokbuild.subprocess.run = old_run
            grokbuild._resolve_exe = old_resolve

        self.assertEqual(len(calls), 4)
        for (cmd, kwargs, cfg), model in zip(calls, ("deepseek-v4-flash", "glm-5.2", "kimi-k2.7-code", "gpt-5.6")):
            spec = grokbuild.OPEN_MODELS[model]
            self.assertEqual(cmd[:4], ["/usr/local/bin/grok", "--no-auto-update", "-p", "do it"])
            self.assertEqual(cmd[cmd.index("--model") + 1], model)
            self.assertNotIn("--agent", cmd)
            self.assertEqual(cmd[cmd.index("--output-format") + 1], "streaming-json")
            self.assertEqual(cmd[cmd.index("--effort") + 1], "medium")
            self.assertEqual(cmd[cmd.index("--reasoning-effort") + 1], "medium")
            self.assertNotIn("--rules", cmd)
            self.assertIn("--always-approve", cmd)
            self.assertNotIn("--no-plan", cmd)
            self.assertNotIn("--no-subagents", cmd)
            self.assertNotIn("--disable-web-search", cmd)
            self.assertNotIn("--no-memory", cmd)
            self.assertEqual(cmd[cmd.index("--cwd") + 1], "/tmp")
            self.assertEqual(kwargs["cwd"], "/tmp")
            self.assertNotEqual(kwargs["env"]["HOME"], os.path.expanduser("~"))
            self.assertEqual(kwargs["env"]["GROK_SUBAGENTS"], "0")
            if model == "gpt-5.6":
                self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
                self.assertEqual(kwargs["env"]["CLIPROXYAPI_API_KEY"], "openbench-local-ingress")
            self.assertIn(f'model = "{spec["model_id"]}"', cfg)
            self.assertIn(f'base_url = "{spec["base_url"]}"', cfg)
            self.assertIn(f'env_key = "{spec["env_key"]}"', cfg)
            parsed = tomllib.loads(cfg)
            self.assertEqual(parsed["model"]["grok-build"]["model"], spec["model_id"])
            self.assertEqual(parsed["model"]["grok-build"]["base_url"], spec["base_url"])
            self.assertEqual(parsed["model"][model]["max_completion_tokens"], 65536)
            self.assertFalse(parsed["subagents"]["enabled"])
            self.assertEqual(parsed["model"][model]["api_backend"], "chat_completions")
            self.assertEqual(parsed["model"][model]["auth_scheme"], "bearer")
            self.assertNotIn("temperature", parsed["model"][model])
            self.assertNotIn("top_p", parsed["model"][model])

    def test_timeout_still_reports_log_usage(self):
        old_run = grokbuild.subprocess.run
        old_resolve = grokbuild._resolve_exe

        def fake_run(cmd, **kwargs):
            home = kwargs["env"]["HOME"]
            log_dir = os.path.join(home, ".grok", "logs")
            os.makedirs(log_dir)
            with open(os.path.join(log_dir, "unified.jsonl"), "w", encoding="utf-8") as fh:
                fh.write(
                    '{"msg":"shell.turn.inference_done",'
                    '"ctx":{"prompt_tokens":100,"cached_prompt_tokens":40,'
                    '"completion_tokens":7,"reasoning_tokens":2}}\n'
                )
            raise grokbuild.subprocess.TimeoutExpired(cmd, 5, output='{"type":"text","data":"partial"}\n')

        grokbuild.subprocess.run = fake_run
        grokbuild._resolve_exe = lambda: "/usr/local/bin/grok"
        try:
            with EnvPatch() as env:
                env["DEEPSEEK_API_KEY"] = "deepseek-test"
                res = grokbuild.run("do it", "/tmp", "deepseek-v4-flash", 5)
        finally:
            grokbuild.subprocess.run = old_run
            grokbuild._resolve_exe = old_resolve

        self.assertFalse(res["completed"])
        self.assertEqual(res["error"], "timeout after 5s")
        self.assertEqual(res["output_tail"], "partial")
        self.assertEqual(res["tokens"], 67)
        self.assertEqual(res["tokens_input_uncached"], 60)
        self.assertEqual(res["tokens_cache_read"], 40)
        self.assertEqual(res["tokens_output"], 7)
        self.assertEqual(res["tokens_reasoning"], 2)


if __name__ == "__main__":
    unittest.main()
