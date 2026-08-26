#!/usr/bin/env python3
"""Tests for the operator-defined open-model registry loader."""

import os
import tempfile
import unittest

from obench import open_models_config as omc

_VALID = """
[models."glm-5.3-flash"]
provider = "openrouter"
model_id = "glm-5.3-flash"
env_key  = "OPENROUTER_API_KEY"
display  = "Z.ai GLM-5.3 Flash"
effort   = "medium"
base_url = "https://openrouter.ai/api/v1"
"""


class LoadOverridesTests(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_missing_file_returns_empty(self):
        self.assertEqual(omc.load_open_model_overrides("/no/such/open_models.toml"), {})

    def test_loads_valid_model(self):
        got = omc.load_open_model_overrides(self._write(_VALID))
        self.assertIn("glm-5.3-flash", got)
        spec = got["glm-5.3-flash"]
        self.assertEqual(spec["provider"], "openrouter")
        self.assertEqual(spec["model_id"], "glm-5.3-flash")
        self.assertEqual(spec["env_key"], "OPENROUTER_API_KEY")
        self.assertEqual(spec["effort"], "medium")
        self.assertEqual(spec["base_url"], "https://openrouter.ai/api/v1")

    def test_base_url_optional(self):
        path = self._write("""
[models."m"]
provider = "openrouter"
model_id = "m"
env_key  = "OPENROUTER_API_KEY"
display  = "M"
effort   = "medium"
""")
        got = omc.load_open_model_overrides(path)
        self.assertEqual(got["m"]["base_url"], "")

    def test_skips_malformed_entry(self):
        # missing required key (env_key) -> skipped, not crashed
        path = self._write("""
[models."bad"]
provider = "openrouter"
model_id = "bad"
display  = "Bad"
effort   = "medium"
""")
        self.assertEqual(omc.load_open_model_overrides(path), {})

    def test_malformed_toml_returns_empty(self):
        path = self._write("this is not valid toml = = =")
        self.assertEqual(omc.load_open_model_overrides(path), {})

    def test_env_var_path_override(self):
        path = self._write(_VALID)
        old = os.environ.get("OPENBENCH_OPEN_MODELS")
        os.environ["OPENBENCH_OPEN_MODELS"] = path
        try:
            got = omc.load_open_model_overrides()  # no explicit path -> uses env
        finally:
            if old is None:
                del os.environ["OPENBENCH_OPEN_MODELS"]
            else:
                os.environ["OPENBENCH_OPEN_MODELS"] = old
        self.assertIn("glm-5.3-flash", got)


class MergeTests(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_merge_adds_new_model(self):
        builtin = {"builtin-a": {"provider": "x"}}
        merged = omc.merge_open_models(builtin, self._write(_VALID))
        self.assertIn("builtin-a", merged)
        self.assertIn("glm-5.3-flash", merged)

    def test_config_overrides_builtin_on_collision(self):
        builtin = {"glm-5.3-flash": {"provider": "BUILTIN", "model_id": "old",
                                     "env_key": "K", "display": "d", "effort": "low"}}
        merged = omc.merge_open_models(builtin, self._write(_VALID))
        self.assertEqual(merged["glm-5.3-flash"]["provider"], "openrouter")

    def test_merge_without_config_returns_builtin(self):
        builtin = {"a": {"provider": "x"}}
        merged = omc.merge_open_models(builtin, "/no/such/file.toml")
        self.assertEqual(merged, builtin)


if __name__ == "__main__":
    unittest.main()
