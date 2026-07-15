#!/usr/bin/env python3
import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTERS = os.path.join(BENCH, "adapters")

import sys
sys.path.insert(0, BENCH)
import candidates


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ADAPTERS, name + ".py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


class Proc:
    returncode = 0
    stdout = ""
    stderr = ""


class CandidateTests(unittest.TestCase):
    def test_v2_variant_matches_ad_hoc_command_and_environment(self):
        helper = load("_codex_ablation")
        spec_path = os.path.join(os.path.dirname(BENCH), "ablation", "codex-home-v2", "candidate.toml")
        with tempfile.TemporaryDirectory() as td:
            auth = os.path.join(td, "auth.json")
            with open(auth, "w", encoding="utf-8") as fh:
                fh.write("{}")
            variant = candidates.load_candidate(spec_path, ADAPTERS)
            variant.auth_files[0]["source"] = auth
            captures = []
            configs = []
            def run(cmd, **kw):
                captures.append((cmd, kw["env"]))
                config_home = kw["env"]["CODEX_HOME"]
                with open(os.path.join(config_home, "config.toml"), encoding="utf-8") as fh:
                    configs.append(fh.read().replace(config_home, "<CONFIG_DIR>"))
                return Proc()
            with mock.patch.object(helper, "_source_codex_home", return_value=td), \
                 mock.patch("subprocess.run", side_effect=run):
                helper.run_variant("codex-v2", "v2", "prompt", td, "gpt-5.6-sol", 9)
                variant.run("prompt", td, "gpt-5.6-sol", 9)
            self.assertEqual(captures[0][0], captures[1][0])
            def normalized(env):
                out = dict(env); out["CODEX_HOME"] = "<CONFIG_DIR>"; return out
            self.assertEqual(normalized(captures[0][1]), normalized(captures[1][1]))
            self.assertEqual(configs[0], configs[1])

    def test_pi_manifest_matches_native_argv_and_environment(self):
        pi = load("pi")
        manifest_path = os.path.join(BENCH, "examples", "pi-harness.toml")
        manifest = candidates.load_candidate(manifest_path, ADAPTERS)
        with tempfile.TemporaryDirectory() as td:
            auth = os.path.join(td, "auth.json")
            with open(auth, "w", encoding="utf-8") as fh:
                fh.write('{"openai-codex":{}}')
            pi._REAL_AUTH = auth
            manifest.auth_files[0]["source"] = auth
            captures = []
            def run(cmd, **kw): captures.append((cmd, kw["env"])); return Proc()
            with mock.patch("subprocess.run", side_effect=run), \
                 mock.patch.object(candidates, "_run_process", side_effect=run):
                pi.run("prompt", td, "gpt-5.5-medium", 9)
                manifest.run("prompt", td, "gpt-5.5-medium", 9)
            self.assertEqual(captures[0][0], captures[1][0])
            def normalized(env):
                out = dict(env)
                home = out["HOME"]
                return {k: v.replace(home, "<HOME>") if isinstance(v, str) else v
                        for k, v in out.items()}
            self.assertEqual(normalized(captures[0][1]), normalized(captures[1][1]))

    def test_manifest_does_not_inherit_host_secrets_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="safe"\ncommand=["cli"]\n')
            manifest = candidates.load_candidate(path, ADAPTERS)
            captured = {}
            def run(cmd, **kw):
                captured.update(kw["env"])
                return Proc()
            with mock.patch.dict(os.environ, {"UNRELATED_API_SECRET": "do-not-forward"}), \
                 mock.patch.object(candidates, "_run_process", side_effect=run):
                manifest.run("prompt", td, "model", 9)
            self.assertNotIn("UNRELATED_API_SECRET", captured)

    def test_manifest_proxy_base_url_override(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    'kind="manifest"\nname="proxy-cli"\ncommand=["cli", "{prompt}"]\n'
                    'base_url_env="CLI_BASE_URL"\nproxy_route="chat/zai/api/paas/v4"\n'
                )
            manifest = candidates.load_candidate(path, ADAPTERS)
            captured = {}
            def run(cmd, **kw):
                captured.update(kw["env"])
                return Proc()
            proxy_env = {
                "OPENBENCH_PROXY": "1",
                "OPENBENCH_PROXY_BASE_URL": "http://127.0.0.1:1234",
                "OPENBENCH_PROXY_CELL_TOKEN": "cell-token",
            }
            with mock.patch.dict(os.environ, proxy_env, clear=False), \
                 mock.patch.object(candidates, "_run_process", side_effect=run):
                manifest.run("prompt", td, "model", 9)
            self.assertEqual(
                captured["CLI_BASE_URL"],
                "http://127.0.0.1:1234/cell/cell-token/chat/zai/api/paas/v4",
            )

    def test_manifest_timeout_kills_descendants(self):
        import time
        with tempfile.TemporaryDirectory() as td:
            marker = os.path.join(td, "survived")
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="timeout"\n'
                         f'command=["/bin/sh", "-c", "(sleep 0.4; touch {marker}) & wait"]\n')
            result = candidates.load_candidate(path, ADAPTERS).run("prompt", td, "model", 0.1)
            self.assertIn("timeout", result["error"])
            time.sleep(0.5)
            self.assertFalse(os.path.exists(marker))

    def test_manifest_model_map_rejects_unknown_canonical(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="pinned"\ncommand=["cli", "{model}"]\n'
                         '[models]\n"known"="cli-known"\n')
            result = candidates.load_candidate(path, ADAPTERS).run("prompt", td, "other", 1)
            self.assertIn("unsupported-model", result["error"])
            self.assertIsNone(result["cmd"])

    def test_manifest_rejects_absolute_auth_source(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="bad"\ncommand=["cli"]\n'
                         '[[auth_files]]\nsource="/tmp/auth"\ndestination="auth"\n')
            with self.assertRaisesRegex(ValueError, "home-relative"):
                candidates.load_candidate(path, ADAPTERS)

    def test_manifest_requires_complete_proxy_pair(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="bad"\ncommand=["cli"]\nproxy_route="chat/zai/v1"\n')
            with self.assertRaisesRegex(ValueError, "base_url_env and proxy_route"):
                candidates.load_candidate(path, ADAPTERS)

    def test_manifest_rejects_auth_destination_escape(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "harness.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="bad"\nisolate_home=true\n'
                         'command=["cli", "{prompt}"]\n'
                         '[[auth_files]]\nsource="~/auth"\ndestination="../auth"\n')
            manifest = candidates.load_candidate(path, ADAPTERS)
            with self.assertRaises(ValueError):
                manifest.run("prompt", td, "model", 1)

    def test_provenance_omits_environment_values(self):
        manifest = candidates.load_candidate(os.path.join(BENCH, "examples", "pi-harness.toml"), ADAPTERS)
        text = json.dumps(manifest.provenance)
        self.assertIn("env_names", text)
        self.assertNotIn("{home}/.pi/agent", text)


if __name__ == "__main__": unittest.main()
