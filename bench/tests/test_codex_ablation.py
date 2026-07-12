#!/usr/bin/env python3
"""Tests for codex_v1/codex_v2 ablation adapter plumbing."""

import importlib.util
import os
import tempfile
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTERS_DIR = os.path.join(BENCH_DIR, "adapters")


def load_adapter(name):
    spec = importlib.util.spec_from_file_location(
        f"test_{name}", os.path.join(ADAPTERS_DIR, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCodexAblationCompose(unittest.TestCase):
    def test_compose_writes_config_instructions_and_auth(self):
        helper = load_adapter("_codex_ablation")
        with tempfile.TemporaryDirectory() as tmp:
            ablation_root = os.path.join(tmp, "ablation")
            variant_dir = os.path.join(ablation_root, "codex-home-v1")
            source_home = os.path.join(tmp, "real-codex")
            runtime_home = os.path.join(tmp, "runtime-codex")
            os.makedirs(variant_dir)
            os.makedirs(source_home)

            with open(os.path.join(variant_dir, "pi-style-instructions.md"), "w", encoding="utf-8") as fh:
                fh.write("tiny instructions\n")
            with open(os.path.join(variant_dir, "config.toml"), "w", encoding="utf-8") as fh:
                fh.write(
                    'model_provider = "deepseek_bridge"\n'
                    'model_reasoning_effort = "medium"\n'
                    'model_instructions_file = "pi-style-instructions.md"\n'
                    'include_environment_context = false\n'
                    '\n[model_providers.deepseek_bridge]\n'
                    'name = "probe only"\n'
                    'base_url = "http://127.0.0.1:4142/v1"\n'
                    'env_key = "DEEPSEEK_API_KEY"\n'
                    'wire_api = "responses"\n'
                )
            with open(os.path.join(source_home, "auth.json"), "w", encoding="utf-8") as fh:
                fh.write('{"fake": true}\n')

            meta = helper.compose_codex_home(
                "v1", runtime_home,
                source_codex_home=source_home,
                ablation_root=ablation_root,
            )

            self.assertEqual(os.path.abspath(runtime_home), meta["codex_home"])
            self.assertTrue(os.path.isfile(meta["config"]))
            self.assertTrue(os.path.isfile(meta["instructions"]))
            self.assertTrue(os.path.isfile(meta["auth"]))
            self.assertEqual(os.path.dirname(meta["auth"]), runtime_home)
            self.assertEqual(os.path.dirname(meta["instructions"]), runtime_home)

            with open(meta["config"], encoding="utf-8") as fh:
                config = fh.read()
            self.assertIn('model_instructions_file = "', config)
            self.assertIn(os.path.abspath(meta["instructions"]), config)
            self.assertIn("include_environment_context = false", config)
            self.assertNotIn("deepseek_bridge", config)
            with open(meta["instructions"], encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "tiny instructions\n")
            with open(meta["auth"], encoding="utf-8") as fh:
                self.assertEqual(fh.read(), '{"fake": true}\n')

    def test_missing_auth_returns_setup_needed(self):
        helper = load_adapter("_codex_ablation")
        with tempfile.TemporaryDirectory() as tmp:
            ablation_root = os.path.join(tmp, "ablation")
            variant_dir = os.path.join(ablation_root, "codex-home-v1")
            os.makedirs(variant_dir)
            with open(os.path.join(variant_dir, "pi-style-instructions.md"), "w", encoding="utf-8") as fh:
                fh.write("tiny instructions\n")
            with open(os.path.join(variant_dir, "config.toml"), "w", encoding="utf-8") as fh:
                fh.write('model_instructions_file = "pi-style-instructions.md"\n')
            with self.assertRaises(FileNotFoundError):
                helper.compose_codex_home(
                    "v1", os.path.join(tmp, "runtime"),
                    source_codex_home=os.path.join(tmp, "missing"),
                    ablation_root=ablation_root,
                )


class TestCodexAblationRegistry(unittest.TestCase):
    def test_variant_adapters_expose_registry_contract(self):
        for name in ("codex_v1", "codex_v2"):
            with self.subTest(name=name):
                mod = load_adapter(name)
                self.assertEqual(mod.NAME, name)
                self.assertIn("gpt-5.5-medium", mod.MODELS)
                self.assertIn("gpt-5.6-sol", mod.MODELS)
                self.assertIn("deepseek-v4-flash", mod.OPEN_MODELS)
                self.assertTrue(callable(mod.run))
                self.assertTrue(callable(mod.version))


if __name__ == "__main__":
    unittest.main()
